from __future__ import annotations

import os

from sqlalchemy import (JSON, BigInteger, Column, DateTime, Float, Index, Integer, MetaData, String, Table, Text,
                        create_engine, update)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert


def _url() -> str:
    url = os.environ.get("DATABASE_URL", "sqlite:///./collector.db")
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


engine = create_engine(_url(), pool_pre_ping=True, future=True)
metadata = MetaData()
Json = JSON().with_variant(JSONB(), "postgresql")

instances = Table(
    "instances", metadata,
    Column("id", String(64), primary_key=True),
    Column("first_seen", DateTime(timezone=True), nullable=False),
    Column("last_seen", DateTime(timezone=True), nullable=False),
    Column("version", String(30)),
    Column("meta", Json),
)

events = Table(
    "events", metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("instance_id", String(64), nullable=False),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("name", String(60), nullable=False),
    Column("user_hash", String(32)),
    Column("props", Json),
    Column("version", String(30)),
    Index("ix_events_name_ts", "name", "ts"),
    Index("ix_events_instance_ts", "instance_id", "ts"),
)

errors = Table(
    "errors", metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("instance_id", String(64), nullable=False),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("source", String(20)),
    Column("kind", String(80)),
    Column("message", Text),
    Column("stack", Json),
    Column("route", String(200)),
    Column("fingerprint", String(32), index=True),
    Column("props", Json),
    Column("version", String(30)),
    Index("ix_errors_ts", "ts"),
)

request_stats = Table(
    "request_stats", metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("instance_id", String(64), nullable=False),
    Column("window_start", DateTime(timezone=True), nullable=False),
    Column("window_end", DateTime(timezone=True), nullable=False),
    Column("route", String(200)),
    Column("method", String(10)),
    Column("status", Integer),
    Column("count", Integer),
    Column("total_ms", Float),
    Column("max_ms", Float),
    Column("version", String(30)),
    Index("ix_request_stats_route", "route", "window_start"),
)


def upsert_instance(conn, iid: str, meta: dict, now) -> None:
    if conn.dialect.name == "postgresql":
        stmt = pg_insert(instances).values(id=iid, first_seen=now, last_seen=now, version=meta.get("version"), meta=meta)
        conn.execute(stmt.on_conflict_do_update(index_elements=["id"], set_={
            "last_seen": now, "version": meta.get("version"), "meta": meta}))
        return
    if conn.execute(update(instances).where(instances.c.id == iid)
                    .values(last_seen=now, version=meta.get("version"), meta=meta)).rowcount == 0:
        conn.execute(instances.insert().values(id=iid, first_seen=now, last_seen=now, version=meta.get("version"),
                                               meta=meta))
