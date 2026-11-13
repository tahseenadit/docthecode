"""Runs before anything opens the database: restore the data directory if needed.

Imported first by app.main. Uses only the environment and the standard library, so it can
run before settings (which read the secret key from the data directory) are loaded.
"""
from __future__ import annotations

import logging
import os
import time
import uuid

from .storage_backup import claim, is_mount, restore

log = logging.getLogger("docthecode.storage")

DATA_DIR = os.environ.get("DOCTHECODE_DATA_DIR", "./data")
BACKUP_DIR = os.environ.get("DOCTHECODE_BACKUP_DIR", "/backup")
RESTORED: dict = {}
# Set by docker/entrypoint.sh when DocTheCode runs the built-in PostgreSQL inside the container.
EMBEDDED_PG_URL = os.environ.get("DATABASE_URL") if os.environ.get("DOCTHECODE_EMBEDDED_DB") == "1" else None
INSTANCE_ID = uuid.uuid4().hex  # this container


def backup_enabled() -> bool:
    """A backup folder counts only if something is mounted there (e.g. a Cloud Storage bucket)."""
    return os.path.isdir(BACKUP_DIR) and (is_mount(BACKUP_DIR) or os.environ.get("DOCTHECODE_BACKUP_DIR") is not None)


def prepare_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not backup_enabled():
        return
    started = time.time()
    used = restore(DATA_DIR, BACKUP_DIR, EMBEDDED_PG_URL)
    claim(BACKUP_DIR, INSTANCE_ID)  # an older container still shutting down must not overwrite our snapshots
    if used:
        RESTORED.update({"from": used, "at": time.time(), "seconds": round(time.time() - started, 2)})
        logging.getLogger("docthecode.storage").warning("Restored DocTheCode data from %s/%s", BACKUP_DIR, used)


prepare_data_dir()
