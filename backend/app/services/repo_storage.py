"""Where doc repo contents live.

Default ("disk"): plain bare git repos under DOCTHECODE_GIT_ROOT on the organization's own
server. Docs are unstructured files with history, diffs and merges, which is exactly what
git is built for; the SQL database only holds metadata. Back up DOCTHECODE_GIT_ROOT together
with the database.

Optional ("database", DOCTHECODE_REPO_STORAGE=database): each repo is also stored in the SQL
database as a versioned git bundle, with the disk as a cache. Use it when the server's
disk is ephemeral (some container hosts) or several DocTheCode servers share one Postgres.
"""
from __future__ import annotations

from functools import lru_cache

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ..config import get_settings
from ..db import SessionLocal
from ..gitstore import GitStore
from ..models import DocRepoBlob
from ..repo_sync import RemoteConflict, SyncedGitStore


class DbBundleStore:
    def version(self, repo_id: str) -> int | None:
        with SessionLocal() as db:
            return db.scalar(select(DocRepoBlob.version).where(DocRepoBlob.repo_id == repo_id))

    def get(self, repo_id: str) -> tuple[int, bytes] | None:
        with SessionLocal() as db:
            row = db.get(DocRepoBlob, repo_id)
            return (row.version, bytes(row.data)) if row else None

    def put(self, repo_id: str, data: bytes, expected_version: int | None) -> int:
        with SessionLocal() as db:
            if expected_version is None:
                db.add(DocRepoBlob(repo_id=repo_id, version=1, data=data))
                try:
                    db.commit()
                except IntegrityError:
                    raise RemoteConflict(f"{repo_id} already exists")
                return 1
            result = db.execute(update(DocRepoBlob)
                                .where(DocRepoBlob.repo_id == repo_id, DocRepoBlob.version == expected_version)
                                .values(version=expected_version + 1, data=data))
            if result.rowcount != 1:
                db.rollback()
                raise RemoteConflict(f"{repo_id} changed (expected v{expected_version})")
            db.commit()
            return expected_version + 1

    def delete(self, repo_id: str) -> None:
        with SessionLocal() as db:
            row = db.get(DocRepoBlob, repo_id)
            if row:
                db.delete(row)
                db.commit()


@lru_cache(maxsize=1)
def shared_git_store() -> GitStore:
    s = get_settings()
    if s.repo_storage == "database":
        return SyncedGitStore(s.git_root, DbBundleStore())
    return GitStore(s.git_root)
