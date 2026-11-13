from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
_is_sqlite = _settings.database_url.startswith("sqlite")
if _is_sqlite and ":///" in _settings.database_url:
    # Local testing without Postgres: make sure the folder for the .db file exists.
    db_file = _settings.database_url.split(":///", 1)[1]
    if db_file and db_file != ":memory:":
        Path(db_file).expanduser().parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(_settings.database_url, pool_pre_ping=True, future=True,
                       connect_args={"check_same_thread": False} if _is_sqlite else {})
if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        # WAL lets readers proceed while one writer commits; busy_timeout makes concurrent
        # writers wait instead of failing. Plenty for one DocTheCode server and a team of users.
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_all() -> None:
    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(engine)
