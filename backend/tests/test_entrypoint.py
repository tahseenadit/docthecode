"""docker/entrypoint.sh with a real PostgreSQL (skipped where PostgreSQL isn't installed)."""
import glob
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[2] / "docker" / "entrypoint.sh"
PG_BIN = next(iter(sorted(glob.glob("/usr/lib/postgresql/*/bin"))), None)
pytestmark = pytest.mark.skipif(not PG_BIN or os.geteuid() != 0, reason="needs PostgreSQL and root")


@pytest.fixture
def env():
    root = tempfile.mkdtemp(prefix="docthecode-ep-")
    os.chmod(root, 0o755)
    e = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    e.update(DOCTHECODE_DATA_DIR=f"{root}/data", DOCTHECODE_PG_SOCKET_DIR=f"{root}/sock", PG_BIN=PG_BIN)
    yield e
    pid = Path(f"{root}/data/postgres/postmaster.pid")
    if pid.exists():  # safety net if a test failed midway
        subprocess.run(["runuser", "-u", "postgres", "--", f"{PG_BIN}/pg_ctl", "-D", f"{root}/data/postgres",
                        "-m", "immediate", "stop"], capture_output=True)
    shutil.rmtree(root, ignore_errors=True)


def run(env, script):
    return subprocess.run([str(ENTRYPOINT), "sh", "-c", script], env=env, capture_output=True, text=True, timeout=120)


def test_first_start_creates_database_and_data_survives_restart(env):
    r = run(env, 'psql "$DATABASE_URL" -tAc "create table t (v text); insert into t values (\'kept\')"; '
                 'echo "embedded=$DOCTHECODE_EMBEDDED_DB"')
    assert r.returncode == 0, r.stderr
    assert "creating the built-in PostgreSQL" in r.stdout and "embedded=1" in r.stdout
    r = run(env, 'psql "$DATABASE_URL" -tAc "select v from t"')
    assert r.returncode == 0 and "kept" in r.stdout and "creating" not in r.stdout
    assert not Path(env["DOCTHECODE_DATA_DIR"], "postgres", "postmaster.pid").exists()  # stopped cleanly


def test_sigterm_reaches_app_before_postgres_stops(env):
    script = ('sleep 60 & S=$!; '
              'trap \'psql "$DATABASE_URL" -tAc "select 1" >/dev/null && echo saved-before-shutdown; kill $S; exit 0\' TERM; '
              'wait')
    p = subprocess.Popen([str(ENTRYPOINT), "sh", "-c", script], env=env, stdout=subprocess.PIPE, text=True)
    time.sleep(5)
    p.send_signal(signal.SIGTERM)
    out, _ = p.communicate(timeout=60)
    assert p.returncode == 0 and "saved-before-shutdown" in out  # DB still up while the app shuts down
    assert not Path(env["DOCTHECODE_DATA_DIR"], "postgres", "postmaster.pid").exists()


def test_external_database_url_skips_built_in_postgres(env):
    r = subprocess.run([str(ENTRYPOINT), "sh", "-c", 'echo "embedded=${DOCTHECODE_EMBEDDED_DB:-no}"'],
                       env={**env, "DATABASE_URL": "postgresql://u@db.internal/docthecode"}, capture_output=True, text=True)
    assert r.stdout.strip() == "embedded=no"
    assert not Path(env["DOCTHECODE_DATA_DIR"], "postgres").exists()


def test_exit_code_passes_through(env):
    assert run(env, "exit 3").returncode == 3


def test_refuses_data_from_another_postgres_major(env):
    assert run(env, "true").returncode == 0
    Path(env["DOCTHECODE_DATA_DIR"], "postgres", "PG_VERSION").write_text("99\n")
    r = run(env, "true")
    assert r.returncode == 1 and "pg_upgrade" in r.stderr
