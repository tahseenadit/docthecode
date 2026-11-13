"""Doc repos persisted in the shared database.

The database is the source of truth for every doc repo: each repo is stored as a
single git bundle with a version number. The bare repo on local disk is only a cache.

  * Before any operation, the local copy is compared with the database version and
    restored from the bundle if it is missing or out of date. So any DocTheCode instance
    (a server, or a teammate running it locally against the same database) sees the
    same docs, and a server with an ephemeral disk loses nothing on restart.
  * After a write, a fresh bundle is saved with a compare-and-swap on the version.
    If another instance wrote in between, the local write is discarded, the newer
    state is restored and the caller gets RefMoved ("reload and try again").

Doc repos are small (Markdown), so re-bundling on every write is cheap.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from functools import wraps
from pathlib import Path
from typing import Callable, Protocol

from .gitstore import GitError, GitStore, RefMoved


class RemoteConflict(Exception):
    """The stored version is not the one we expected."""


class BundleStore(Protocol):
    def version(self, repo_id: str) -> int | None: ...
    def get(self, repo_id: str) -> tuple[int, bytes] | None: ...
    def put(self, repo_id: str, data: bytes, expected_version: int | None) -> int: ...
    def delete(self, repo_id: str) -> None: ...


class MemoryBundleStore:
    """In-memory BundleStore, used by tests."""

    def __init__(self):
        self.items: dict[str, tuple[int, bytes]] = {}
        self._lock = threading.Lock()

    def version(self, repo_id):
        item = self.items.get(repo_id)
        return item[0] if item else None

    def get(self, repo_id):
        return self.items.get(repo_id)

    def put(self, repo_id, data, expected_version):
        with self._lock:
            current = self.version(repo_id)
            if current != expected_version:
                raise RemoteConflict(f"{repo_id}: expected v{expected_version}, found v{current}")
            new = (current or 0) + 1
            self.items[repo_id] = (new, data)
            return new

    def delete(self, repo_id):
        self.items.pop(repo_id, None)


_READS = ("exists", "branch_sha", "resolve", "list_branches", "list_files", "read_file", "read_many", "log",
          "merge_base", "changed_files", "diff", "can_fast_forward")
_WRITES = ("init_repo", "create_branch", "reset_branch", "delete_branch", "commit", "merge")
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock_for(repo_id: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(repo_id, threading.RLock())


class SyncedGitStore(GitStore):
    """A GitStore whose repos live in a BundleStore (the database)."""

    VERSION_FILE = "docthecode-version"

    def __init__(self, root, remote: BundleStore):
        super().__init__(root)
        self.remote = remote
        self._tl = threading.local()

    # ---------------------------------------------------------------- versions
    def _local_version(self, repo_id: str) -> int | None:
        f = self.path(repo_id) / self.VERSION_FILE
        try:
            return int(f.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None

    def _set_local_version(self, repo_id: str, v: int) -> None:
        (self.path(repo_id) / self.VERSION_FILE).write_text(str(v))

    # ---------------------------------------------------------------- sync
    def _ensure_fresh(self, repo_id: str) -> None:
        remote_v = self.remote.version(repo_id)
        local_exists = self.path(repo_id).exists()
        if remote_v is None:
            if local_exists and self._local_version(repo_id) is None:
                self._publish(repo_id)  # a repo created before DB storage existed: upload it once
            elif local_exists:
                # It was deleted elsewhere; drop the stale cache.
                shutil.rmtree(self.path(repo_id), ignore_errors=True)
            return
        if not local_exists or self._local_version(repo_id) != remote_v:
            self._restore(repo_id)

    def _restore(self, repo_id: str) -> None:
        item = self.remote.get(repo_id)
        target = self.path(repo_id)
        if item is None:
            shutil.rmtree(target, ignore_errors=True)
            return
        version, data = item
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            bundle = Path(tmp) / "repo.bundle"
            bundle.write_bytes(data)
            staging = Path(tmp) / "repo.git"
            subprocess.run(["git", "init", "-q", "--bare", str(staging)], check=True, capture_output=True)
            proc = subprocess.run(["git", f"--git-dir={staging}", "fetch", "-q", str(bundle), "+refs/heads/*:refs/heads/*"],
                                  capture_output=True)
            if proc.returncode != 0:
                raise GitError(f"Could not restore {repo_id}: {proc.stderr.decode(errors='replace')}")
            (staging / self.VERSION_FILE).write_text(str(version))
            shutil.rmtree(target, ignore_errors=True)
            os.replace(staging, target)

    def _publish(self, repo_id: str) -> None:
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            bundle = Path(tmp) / "repo.bundle"
            proc = subprocess.run(["git", f"--git-dir={self.path(repo_id)}", "bundle", "create", "-q", str(bundle),
                                   "--branches"], capture_output=True)
            if proc.returncode != 0:
                raise GitError(f"Could not bundle {repo_id}: {proc.stderr.decode(errors='replace')}")
            data = bundle.read_bytes()
        try:
            new_v = self.remote.put(repo_id, data, self._local_version(repo_id))
        except RemoteConflict:
            self._restore(repo_id)
            raise RefMoved("Someone else changed these docs at the same moment. Reload and try again.")
        self._set_local_version(repo_id, new_v)

    # ---------------------------------------------------------------- wrapping
    def _synced(self, repo_id: str, fn: Callable, write: bool):
        tl = self._tl
        with _lock_for(repo_id):
            outer = getattr(tl, "depth", 0) == 0
            if outer:
                tl.dirty = False
                self._ensure_fresh(repo_id)
            tl.depth = getattr(tl, "depth", 0) + 1
            try:
                result = fn()
                if write:
                    tl.dirty = True
            finally:
                tl.depth -= 1
            if outer and tl.dirty:
                tl.dirty = False
                self._publish(repo_id)
            return result

    def delete_repo(self, repo_id: str) -> None:
        with _lock_for(repo_id):
            self.remote.delete(repo_id)
            super().delete_repo(repo_id)


def _make_wrapper(name: str, write: bool):
    base = getattr(GitStore, name)

    @wraps(base)
    def wrapper(self, repo_id, *args, **kwargs):
        return self._synced(repo_id, lambda: base(self, repo_id, *args, **kwargs), write)

    return wrapper


for _name in _READS:
    setattr(SyncedGitStore, _name, _make_wrapper(_name, False))
for _name in _WRITES:
    setattr(SyncedGitStore, _name, _make_wrapper(_name, True))
