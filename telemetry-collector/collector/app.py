"""DocTheCode telemetry collector: the only thing that talks to your online database.

DocTheCode servers (running inside customers' networks) POST anonymous batches here. The
collector validates them, rate-limits per instance and writes them to Postgres. Customer
servers never get database credentials; they only hold a write-only ingest key.

Environment:
  DATABASE_URL            your online Postgres (Neon, Supabase, ...)
  COLLECTOR_INGEST_KEYS   comma-separated keys DocTheCode servers may use (rotate by adding a new one)
  COLLECTOR_ADMIN_TOKEN   bearer token for the read-only /v1/summary endpoint
"""
from __future__ import annotations

import hmac
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, insert, select

from .db import engine, errors, events, instances, metadata, request_stats, upsert_instance

MAX_BODY = 1_000_000
BATCHES_PER_HOUR = 240


def _keys() -> list[str]:
    return [k.strip() for k in os.environ.get("COLLECTOR_INGEST_KEYS", "").split(",") if k.strip()]


@asynccontextmanager
async def lifespan(_: FastAPI):
    metadata.create_all(engine)
    yield


app = FastAPI(title="DocTheCode telemetry collector", lifespan=lifespan, docs_url=None, redoc_url=None)


# ------------------------------------------------------------------ payload schema (bounded)
class Instance(BaseModel):
    id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    version: str = Field(max_length=30)
    python: str = Field(default="", max_length=20)
    os: str = Field(default="", max_length=20)
    db: str = Field(default="", max_length=20)
    storage: str = Field(default="", max_length=20)


class Event(BaseModel):
    name: str = Field(max_length=60)
    ts: float
    user: str | None = Field(default=None, max_length=32)
    props: dict = Field(default_factory=dict)


class Error(BaseModel):
    ts: float
    source: str = Field(max_length=20)
    kind: str = Field(max_length=80)
    message: str = Field(default="", max_length=400)
    stack: list[str] = Field(default_factory=list, max_length=30)
    route: str = Field(default="", max_length=200)
    fingerprint: str = Field(max_length=32)
    props: dict = Field(default_factory=dict)


class RequestStat(BaseModel):
    route: str = Field(max_length=200)
    method: str = Field(max_length=10)
    status: int
    count: int = Field(ge=0)
    total_ms: float = Field(ge=0)
    max_ms: float = Field(ge=0)


class Window(BaseModel):
    start: float
    end: float


class Batch(BaseModel):
    schema_: int = Field(alias="schema")
    sent_at: float
    instance: Instance
    events: list[Event] = Field(default_factory=list, max_length=2000)
    errors: list[Error] = Field(default_factory=list, max_length=300)
    request_stats: list[RequestStat] = Field(default_factory=list, max_length=2000)
    window: Window


# ------------------------------------------------------------------ rate limiting
_recent: dict[str, deque] = defaultdict(deque)


def _allow(instance_id: str) -> bool:
    now = time.time()
    q = _recent[instance_id]
    while q and q[0] < now - 3600:
        q.popleft()
    if len(q) >= BATCHES_PER_HOUR:
        return False
    q.append(now)
    return True


def _ts(t: float) -> datetime:
    # Clamp clocks that are wildly off so one bad server can't skew time-based queries.
    now = time.time()
    return datetime.fromtimestamp(min(max(t, now - 30 * 86400), now + 300), tz=timezone.utc)


def _props(p: dict) -> dict:
    return {str(k)[:40]: v for k, v in list(p.items())[:20]
            if isinstance(v, (bool, int, float)) or v is None or (isinstance(v, str) and len(v) <= 64)}


# ------------------------------------------------------------------ endpoints
@app.post("/v1/batch", status_code=202)
async def ingest(request: Request, x_docthecode_key: str = Header(default="")):
    if not any(hmac.compare_digest(x_docthecode_key, k) for k in _keys()):
        raise HTTPException(401, "bad ingest key")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413, "batch too large")
    batch = Batch.model_validate_json(raw)
    iid = batch.instance.id
    if not _allow(iid):
        raise HTTPException(429, "slow down")
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        upsert_instance(conn, iid, batch.instance.model_dump(), now)
        if batch.events:
            conn.execute(insert(events), [
                {"instance_id": iid, "ts": _ts(e.ts), "name": e.name, "user_hash": e.user, "props": _props(e.props),
                 "version": batch.instance.version} for e in batch.events])
        if batch.errors:
            conn.execute(insert(errors), [
                {"instance_id": iid, "ts": _ts(e.ts), "source": e.source, "kind": e.kind, "message": e.message,
                 "stack": e.stack, "route": e.route, "fingerprint": e.fingerprint, "props": _props(e.props),
                 "version": batch.instance.version} for e in batch.errors])
        if batch.request_stats:
            start, end = _ts(batch.window.start), _ts(batch.window.end)
            conn.execute(insert(request_stats), [
                {"instance_id": iid, "window_start": start, "window_end": end, "route": r.route, "method": r.method,
                 "status": r.status, "count": r.count, "total_ms": r.total_ms, "max_ms": r.max_ms,
                 "version": batch.instance.version} for r in batch.request_stats])
    return {"accepted": len(batch.events) + len(batch.errors) + len(batch.request_stats)}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/v1/summary")
def summary(authorization: str = Header(default="")):
    """Quick numbers for the last 7 days. For real analysis, see sql/analytics.sql."""
    token = os.environ.get("COLLECTOR_ADMIN_TOKEN", "")
    if not token or not hmac.compare_digest(authorization, f"Bearer {token}"):
        raise HTTPException(401, "admin token required")
    since = datetime.now(timezone.utc) - timedelta(days=7)
    with engine.connect() as conn:
        top_events = conn.execute(select(events.c.name, func.count()).where(events.c.ts >= since)
                                  .group_by(events.c.name).order_by(func.count().desc()).limit(25)).all()
        top_errors = conn.execute(select(errors.c.kind, errors.c.fingerprint, errors.c.route, func.count())
                                  .where(errors.c.ts >= since)
                                  .group_by(errors.c.kind, errors.c.fingerprint, errors.c.route)
                                  .order_by(func.count().desc()).limit(15)).all()
        return {
            "instances_active_7d": conn.scalar(select(func.count()).select_from(instances)
                                               .where(instances.c.last_seen >= since)),
            "users_active_7d": conn.scalar(select(func.count(func.distinct(events.c.user_hash)))
                                           .where(events.c.ts >= since)),
            "events": {name: n for name, n in top_events},
            "errors": [{"kind": k, "fingerprint": f, "route": r, "count": n} for k, f, r, n in top_errors],
        }
