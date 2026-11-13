"""Keeps a snapshot of the data directory in the mounted backup folder (if there is one)."""
from __future__ import annotations

import asyncio
import logging
import time

from .. import bootstrap
from ..config import get_settings
from ..storage_backup import describe, fingerprint, is_mount, owns, pg_change_counter, snapshot

log = logging.getLogger("docthecode.storage")
INTERVAL = 30  # seconds between change checks

_state: dict = {"last_at": None, "last_error": None, "last_size": None, "last_print": None}


def storage_status() -> dict:
    s = get_settings()
    data_persistent = is_mount(s.data_dir)
    backup = bootstrap.backup_enabled()
    return {
        "data_dir": s.data_dir,
        "data_persistent": data_persistent,
        "database": "built-in PostgreSQL" if bootstrap.EMBEDDED_PG_URL else
                    ("SQLite (development)" if s.uses_bundled_sqlite else "external"),
        "external_database": not (bootstrap.EMBEDDED_PG_URL or s.uses_bundled_sqlite),
        "backup_enabled": backup,
        "backup_dir": bootstrap.BACKUP_DIR if backup else None,
        "last_backup_at": _state["last_at"],
        "last_backup_error": _state["last_error"],
        "snapshot": describe(bootstrap.BACKUP_DIR) if backup else None,
        "restored": bootstrap.RESTORED or None,
        # Data survives a restart if it's on a real volume, or if snapshots go to a mounted folder.
        "safe": data_persistent or backup,
    }


def _fingerprint() -> tuple:
    pg = pg_change_counter(bootstrap.EMBEDDED_PG_URL) if bootstrap.EMBEDDED_PG_URL else None
    return fingerprint(get_settings().data_dir), pg


def backup_now(force: bool = False) -> bool:
    if not bootstrap.backup_enabled():
        return False
    if not owns(bootstrap.BACKUP_DIR, bootstrap.INSTANCE_ID):
        _state["last_error"] = "A newer DocTheCode container has taken over the backup folder; this one stopped saving."
        return False
    s = get_settings()
    fp = _fingerprint()
    if not force and fp == _state["last_print"]:
        return False
    try:
        _state["last_size"] = snapshot(s.data_dir, bootstrap.BACKUP_DIR, bootstrap.EMBEDDED_PG_URL)
        _state.update(last_at=time.time(), last_error=None, last_print=fp)
        return True
    except Exception as e:
        _state["last_error"] = str(e)[:300]
        log.exception("snapshot to %s failed", bootstrap.BACKUP_DIR)
        return False


async def run_forever() -> None:
    if not bootstrap.backup_enabled():
        return
    _state["last_print"] = _fingerprint()  # just restored or freshly started
    while True:
        await asyncio.sleep(INTERVAL)
        await asyncio.to_thread(backup_now)
