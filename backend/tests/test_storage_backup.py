import os
import sqlite3
import stat

from app.gitstore import Author, GitStore
from app.storage_backup import (PREVIOUS, SNAPSHOT, claim, ensure_secret_key, fingerprint, is_mount, owns, restore,
                                snapshot)


def make_data(d):
    db = sqlite3.connect(d / "docthecode.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("create table t (v text)")
    db.execute("insert into t values ('hello')")
    db.commit()  # keep the connection open: data may still sit in the WAL file
    GitStore(d / "repos").init_repo("space", "main", Author("a", "a@x"), {"index.md": "# Home\n"})
    key = ensure_secret_key(d)
    return db, key


def test_snapshot_and_restore_into_a_fresh_container(tmp_path):
    data, backup, fresh = tmp_path / "data", tmp_path / "bucket", tmp_path / "new-container"
    data.mkdir()
    conn, key = make_data(data)
    assert snapshot(data, backup) > 0
    conn.execute("insert into t values ('after snapshot')")
    conn.commit()
    snapshot(data, backup)  # second snapshot rotates the first to .prev
    assert (backup / SNAPSHOT).exists() and (backup / PREVIOUS).exists()

    assert restore(fresh, backup) == SNAPSHOT
    rows = [r[0] for r in sqlite3.connect(fresh / "docthecode.db").execute("select v from t")]
    assert rows == ["hello", "after snapshot"]
    assert GitStore(fresh / "repos").read_file("space", "main", "index.md") == "# Home\n"
    assert ensure_secret_key(fresh) == key  # credentials stay decryptable after a restore


def test_restore_never_overwrites_existing_data(tmp_path):
    data, backup = tmp_path / "data", tmp_path / "bucket"
    data.mkdir()
    make_data(data)
    snapshot(data, backup)
    assert restore(data, backup) is None


def test_damaged_snapshot_falls_back_to_previous(tmp_path):
    data, backup, fresh = tmp_path / "data", tmp_path / "bucket", tmp_path / "fresh"
    data.mkdir()
    make_data(data)
    snapshot(data, backup)
    snapshot(data, backup)
    (backup / SNAPSHOT).write_bytes(b"not a tarball")
    assert restore(fresh, backup) == PREVIOUS
    assert (fresh / "docthecode.db").exists()


def test_no_backup_folder(tmp_path):
    assert restore(tmp_path / "data", tmp_path / "missing") is None


def test_secret_key_is_generated_once_and_private(tmp_path):
    k1 = ensure_secret_key(tmp_path)
    assert len(k1) == 64 and ensure_secret_key(tmp_path) == k1
    assert stat.S_IMODE(os.stat(tmp_path / ".secret_key").st_mode) == 0o600


def test_fingerprint_changes_when_data_changes(tmp_path):
    (tmp_path / "a").write_text("1")
    before = fingerprint(tmp_path)
    (tmp_path / "b").write_text("22")
    assert fingerprint(tmp_path) != before


def test_is_mount():
    assert is_mount("/")
    assert not is_mount("/definitely/not/here")


def test_newer_container_takes_over_backups(tmp_path):
    assert owns(tmp_path, "old")  # nobody has claimed yet
    claim(tmp_path, "old")
    claim(tmp_path, "new")  # redeploy: the new container starts while the old one shuts down
    assert owns(tmp_path, "new") and not owns(tmp_path, "old")
