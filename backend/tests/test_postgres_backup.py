"""Snapshot and restore with a real PostgreSQL server (skipped if PostgreSQL isn't installed)."""
import glob
import os
import shutil
import subprocess
import tempfile

import pytest

from app.gitstore import Author, GitStore
from app.storage_backup import ensure_secret_key, pg_change_counter, restore, snapshot

PG_BIN = next(iter(sorted(glob.glob("/usr/lib/postgresql/*/bin"))), None)
pytestmark = pytest.mark.skipif(not PG_BIN or os.geteuid() != 0 or not shutil.which("runuser"),
                                reason="needs PostgreSQL server binaries and root (to run as the postgres user)")


def as_pg(*cmd):
    return subprocess.run(["runuser", "-u", "postgres", "--", *cmd], check=True, capture_output=True)


@pytest.fixture
def pg_server():
    """Start a throwaway PostgreSQL cluster, like docker/entrypoint.sh does, and yield a factory for databases."""
    root = tempfile.mkdtemp(prefix="docthecode-pg-")
    os.chmod(root, 0o755)
    pgdata, sock = os.path.join(root, "pgdata"), os.path.join(root, "sock")
    for d in (pgdata, sock):  # like docker/entrypoint.sh: create as root, hand over to postgres
        os.makedirs(d)
        shutil.chown(d, "postgres", "postgres")
    os.chmod(pgdata, 0o700)
    as_pg(f"{PG_BIN}/initdb", "-D", pgdata, "-U", "docthecode", "--auth-local=trust", "--auth-host=reject", "--no-locale")
    as_pg(f"{PG_BIN}/pg_ctl", "-D", pgdata, "-w", "-l", f"{pgdata}/server.log", "start",
          "-o", f"-c listen_addresses='' -c unix_socket_directories='{sock}'")
    env = {**os.environ, "PATH": f"{PG_BIN}:{os.environ['PATH']}"}
    old_path = os.environ["PATH"]
    os.environ["PATH"] = env["PATH"]

    def new_db(name):
        as_pg(f"{PG_BIN}/createdb", "-h", sock, "-U", "docthecode", name)
        return f"postgresql://docthecode@/{name}?host={sock}"

    try:
        yield new_db
    finally:
        os.environ["PATH"] = old_path
        as_pg(f"{PG_BIN}/pg_ctl", "-D", pgdata, "-m", "immediate", "stop")
        shutil.rmtree(root, ignore_errors=True)


def psql(url, sql):
    return subprocess.run(["psql", "--dbname", url, "-tAc", sql], check=True, capture_output=True).stdout.decode().strip()


def test_snapshot_and_restore_built_in_postgres(pg_server, tmp_path):
    old_db, new_db = pg_server("docthecode_old"), pg_server("docthecode_new")
    data, bucket, fresh = tmp_path / "data", tmp_path / "bucket", tmp_path / "fresh"
    psql(old_db, "create table doc_repos (id text primary key, name text); insert into doc_repos values ('r1', 'payments')")
    GitStore(data / "repos").init_repo("r1", "main", Author("a", "a@x"), {"index.md": "# Payments\n"})
    key = ensure_secret_key(data)

    before = pg_change_counter(old_db)
    psql(old_db, "insert into doc_repos values ('r2', 'billing')")
    assert pg_change_counter(old_db) != before  # changes are detected

    assert snapshot(data, bucket, old_db) > 0

    # A new container: empty data folder, empty database.
    assert restore(fresh, bucket, new_db) is not None
    assert psql(new_db, "select string_agg(name, ',' order by id) from doc_repos") == "payments,billing"
    assert GitStore(fresh / "repos").read_file("r1", "main", "index.md") == "# Payments\n"
    assert ensure_secret_key(fresh) == key
    assert not (fresh / "docthecode.pgdump").exists()


def test_restore_never_touches_a_database_with_data(pg_server, tmp_path):
    src, target = pg_server("docthecode_src"), pg_server("docthecode_target")
    psql(src, "create table t (v text); insert into t values ('from backup')")
    data, bucket = tmp_path / "data", tmp_path / "bucket"
    data.mkdir()
    snapshot(data, bucket, src)
    psql(target, "create table t (v text); insert into t values ('live data')")
    assert restore(tmp_path / "fresh", bucket, target) is None
    assert psql(target, "select v from t") == "live data"
