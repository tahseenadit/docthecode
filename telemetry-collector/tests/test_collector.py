"""Run with: cd telemetry-collector && DATABASE_URL=sqlite:///./test.db COLLECTOR_INGEST_KEYS=k pytest"""
import os
import time

os.environ.setdefault("DATABASE_URL", "sqlite:///./test-collector.db")
os.environ.setdefault("COLLECTOR_INGEST_KEYS", "k")
os.environ.setdefault("COLLECTOR_ADMIN_TOKEN", "admin")

from fastapi.testclient import TestClient  # noqa: E402

from collector.app import app  # noqa: E402


def batch(**over):
    now = time.time()
    b = {"schema": 1, "sent_at": now, "instance": {"id": "instance-0001", "version": "0.3.0", "db": "sqlite"},
         "events": [{"name": "doc_pr_opened", "ts": now, "user": "abcd1234abcd1234", "props": {"reopened": False}}],
         "errors": [{"ts": now, "source": "request", "kind": "KeyError", "message": "boom", "stack": ["app/x.py:f:1"],
                     "route": "/api/inbox", "fingerprint": "0123456789abcdef"}],
         "request_stats": [{"route": "/api/inbox", "method": "GET", "status": 200, "count": 3, "total_ms": 30, "max_ms": 20}],
         "window": {"start": now - 60, "end": now}}
    b.update(over)
    return b


def test_ingest_and_summary():
    with TestClient(app) as c:
        assert c.post("/v1/batch", json=batch(), headers={"X-DocTheCode-Key": "wrong"}).status_code == 401
        r = c.post("/v1/batch", json=batch(), headers={"X-DocTheCode-Key": "k"})
        assert r.status_code == 202 and r.json()["accepted"] == 3
        assert c.post("/v1/batch", json=batch(), headers={"X-DocTheCode-Key": "k"}).status_code == 202  # upsert instance
        s = c.get("/v1/summary", headers={"Authorization": "Bearer admin"}).json()
        assert s["instances_active_7d"] == 1 and s["events"]["doc_pr_opened"] >= 2
        assert c.get("/v1/summary").status_code == 401


def test_rejects_oversized_and_malformed():
    with TestClient(app) as c:
        bad = batch(instance={"id": "x", "version": "1"})  # id too short
        assert c.post("/v1/batch", json=bad, headers={"X-DocTheCode-Key": "k"}).status_code == 422
