"""Snapshot and restore DocTheCode's data directory (standard library only).

DocTheCode keeps everything in one data directory (default /data in the Docker image):
the built-in PostgreSQL database, the doc repos and the auto-generated secret key. On hosts with a
persistent volume that is all you need. On hosts whose container disk is wiped on every
restart (many "serverless container" platforms), mount a bucket or
network folder at /backup: DocTheCode then writes a consistent snapshot there every few
seconds after a change and restores it automatically on start.

The snapshot is written as one file, never edited in place, because bucket mounts
(Cloud Storage FUSE, S3 mounts and similar) don't support file locking or partial writes,
so a live database must never run directly on such a mount.
"""
from __future__ import annotations

import io
import os
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

OWNER = "docthecode-owner"
SNAPSHOT = "docthecode-backup.tar.gz"
PREVIOUS = "docthecode-backup.prev.tar.gz"
DB_FILE = "docthecode.db"          # SQLite (development without Docker)
PG_DUMP_FILE = "docthecode.pgdump"  # built-in PostgreSQL (the Docker image)
SECRET_FILE = ".secret_key"
REPOS_DIR = "repos"


def is_mount(path: str | os.PathLike) -> bool:
    """True when `path` is its own mount point (a Docker volume, a bucket mount, a disk)."""
    p = os.path.realpath(path)
    if not os.path.isdir(p):
        return False
    if os.path.ismount(p):
        return True
    try:
        with open("/proc/mounts") as f:
            return any(line.split()[1] == p for line in f if len(line.split()) > 1)
    except OSError:
        return False


def fingerprint(data_dir: str | os.PathLike, skip: tuple[str, ...] = ("postgres",)) -> tuple:
    """Cheap 'has anything changed?' signature of the data files: newest mtime, total size, count.

    The live PostgreSQL folder is skipped (it changes constantly); use pg_change_counter for it.
    """
    newest, total, count = 0.0, 0, 0
    root_dir = os.path.abspath(data_dir)
    for root, dirs, files in os.walk(root_dir):
        if root == root_dir:
            dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            try:
                st = os.stat(os.path.join(root, name))
            except FileNotFoundError:
                continue
            newest, total, count = max(newest, st.st_mtime), total + st.st_size, count + 1
    return round(newest, 3), total, count


def pg_change_counter(pg_url: str) -> str:
    """Total rows ever inserted/updated/deleted in DocTheCode's tables: changes whenever data changes."""
    proc = subprocess.run(["psql", "--dbname", pg_url, "-tAc",
                           "SELECT coalesce(sum(n_tup_ins + n_tup_upd + n_tup_del), 0) FROM pg_stat_user_tables"],
                          capture_output=True)
    return proc.stdout.decode().strip() if proc.returncode == 0 else f"error:{time.time()}"


def _pg_dump(pg_url: str, out: Path) -> None:
    proc = subprocess.run(["pg_dump", "--format=custom", "--no-owner", "--no-privileges", "--file", str(out),
                           "--dbname", pg_url], capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError("pg_dump failed: " + proc.stderr.decode(errors="replace")[-500:])


def _pg_is_empty(pg_url: str) -> bool:
    proc = subprocess.run(["psql", "--dbname", pg_url, "-tAc",
                           "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"],
                          capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError("psql failed: " + proc.stderr.decode(errors="replace")[-500:])
    return proc.stdout.decode().strip() == "0"


def _pg_restore(pg_url: str, dump: Path) -> None:
    proc = subprocess.run(["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error",
                           "--dbname", pg_url, str(dump)], capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError("pg_restore failed: " + proc.stderr.decode(errors="replace")[-500:])


def snapshot(data_dir: str | os.PathLike, backup_dir: str | os.PathLike, pg_url: str | None = None) -> int:
    """Write a consistent snapshot of DocTheCode's data into backup_dir. Returns its size in bytes.

    Includes the database (a pg_dump of the built-in Postgres when `pg_url` is given, otherwise the
    SQLite file used in development), the doc repos and the secret key.
    """
    data_dir, backup_dir = Path(data_dir), Path(backup_dir)
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if pg_url:
            dump = Path(tmp) / PG_DUMP_FILE
            _pg_dump(pg_url, dump)  # a consistent snapshot while DocTheCode keeps running
            tar.add(dump, arcname=PG_DUMP_FILE)
        elif (data_dir / DB_FILE).exists():
            # SQLite's online backup copies a consistent state even while DocTheCode keeps writing.
            copy = Path(tmp) / DB_FILE
            src, dst = sqlite3.connect(data_dir / DB_FILE), sqlite3.connect(copy)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            tar.add(copy, arcname=DB_FILE)
        if (data_dir / SECRET_FILE).exists():
            tar.add(data_dir / SECRET_FILE, arcname=SECRET_FILE)
        if (data_dir / REPOS_DIR).is_dir():
            tar.add(data_dir / REPOS_DIR, arcname=REPOS_DIR)
    data = buf.getvalue()

    backup_dir.mkdir(parents=True, exist_ok=True)
    partial = backup_dir / (SNAPSHOT + ".partial")
    with open(partial, "wb") as f:  # one sequential write + close: what bucket mounts handle well
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    current = backup_dir / SNAPSHOT
    if current.exists():
        os.replace(current, backup_dir / PREVIOUS)
    os.replace(partial, current)
    return len(data)


def restore(data_dir: str | os.PathLike, backup_dir: str | os.PathLike, pg_url: str | None = None) -> str | None:
    """On a fresh container (no secret key yet), unpack the latest snapshot. Returns the snapshot used.

    Never overwrites existing data: the database is only restored into an empty database.
    """
    data_dir, backup_dir = Path(data_dir), Path(backup_dir)
    if (data_dir / SECRET_FILE).exists() or not backup_dir.is_dir():
        return None
    if pg_url and not _pg_is_empty(pg_url):
        return None
    for name in (SNAPSHOT, PREVIOUS):
        path = backup_dir / name
        if not path.exists():
            continue
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=data_dir) as tmp:
                with tarfile.open(path, "r:gz") as tar:
                    tar.extractall(tmp, filter="data")  # refuses absolute paths, .., devices, links out
                dump = Path(tmp) / PG_DUMP_FILE
                if pg_url and dump.exists():
                    _pg_restore(pg_url, dump)
                for entry in os.listdir(tmp):
                    if entry == PG_DUMP_FILE or (entry == DB_FILE and pg_url):
                        continue
                    target = data_dir / entry
                    if not target.exists():
                        os.replace(os.path.join(tmp, entry), target)
            return name
        except (tarfile.TarError, OSError, EOFError):
            continue  # damaged snapshot: try the previous one
    return None


def ensure_secret_key(data_dir: str | os.PathLike) -> str:
    """Read the server's secret key from the data directory, creating it on first start."""
    import secrets

    path = Path(data_dir) / SECRET_FILE
    if path.exists():
        key = path.read_text().strip()
        if len(key) >= 32:
            return key
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    key = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    return key


def describe(backup_dir: str | os.PathLike) -> dict:
    p = Path(backup_dir) / SNAPSHOT
    if not p.exists():
        return {"exists": False}
    st = p.stat()
    return {"exists": True, "size": st.st_size, "modified": st.st_mtime, "age_s": round(time.time() - st.st_mtime)}


def claim(backup_dir: str | os.PathLike, instance_id: str) -> None:
    """Mark this container as the one allowed to write snapshots (the newest one to start)."""
    Path(backup_dir, OWNER).write_text(instance_id)


def owns(backup_dir: str | os.PathLike, instance_id: str) -> bool:
    """False once a newer container (e.g. after a redeploy) has taken over the backup folder."""
    try:
        return Path(backup_dir, OWNER).read_text().strip() == instance_id
    except FileNotFoundError:
        return True
