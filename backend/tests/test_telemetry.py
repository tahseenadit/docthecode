import asyncio

from app.services.telemetry import Telemetry, bucket, clean_props, frames, scrub


def make(enabled=True, url="https://collector.example"):
    sent = []

    async def send(u, k, batch):
        sent.append((u, k, batch))

    t = Telemetry(url=url, key="ingest", secret="s" * 32, version="9.9", enabled=lambda: enabled,
                  instance=lambda: {"id": "inst1", "db": "sqlite"}, send=send)
    return t, sent


def test_scrub_removes_identifying_data():
    msg = ("token ghp_abcdefghijklmnop1234 for ada@acme.com at https://github.acme.com/acme/payments "
           "file /Users/ada/acme/docs/secret.md sha 0123456789abcdef0123456789abcdef key sk-ant-api03-xyzxyzxyzxyz "
           "'Payments API rate limits for enterprise customers'")
    out = scrub(msg)
    for leak in ("ghp_", "ada@", "acme", "secret.md", "0123456789abcdef", "sk-ant", "Payments API"):
        assert leak not in out, leak
    assert "<github-token>" in out and "<email>" in out and "<url>" in out


def test_props_only_small_scalars():
    props = clean_props({"state": "approved", "turns": 7, "ok": True, "title": "Fix the payments page for ACME!",
                         "nested": {"a": 1}, "list": [1], "Weird-Key": 1.5})
    assert props == {"state": "approved", "turns": 7, "ok": True, "weird_key": 1.5}


def test_user_ids_are_hashed_and_stable():
    t, _ = make()
    t.track("login", user="user-123")
    t.track("login", user="user-123")
    h1, h2 = (e["user"] for e in t.events)
    assert h1 == h2 and "user-123" not in h1 and len(h1) == 16
    other, _ = make()
    other.secret = "t" * 32
    assert other.user_hash("user-123") != h1  # vendor can't correlate across servers


def test_request_stats_are_aggregated():
    t, _ = make()
    for ms in (10, 30, 20):
        t.record_request("/api/repos/{owner}/{name}", "GET", 200, ms)
    t.record_request("/api/repos/{owner}/{name}", "GET", 404, 5)
    stats = {(s["status"]): s for s in t.build_batch()["request_stats"]}
    assert stats[200]["count"] == 3 and stats[200]["total_ms"] == 60 and stats[200]["max_ms"] == 30
    assert stats[404]["count"] == 1


def test_errors_have_frames_and_fingerprint():
    t, _ = make()
    try:
        {}["missing key for https://acme.example"]
    except KeyError as e:
        t.record_exception(e, "request", route="/api/inbox")
    err = t.errors[0]
    assert err["kind"] == "KeyError" and "acme" not in err["message"]
    assert err["stack"] and all(":" in s for s in err["stack"])
    assert not any(s.startswith("/") for s in err["stack"])
    assert len(err["fingerprint"]) == 16


def test_frames_have_no_filesystem_paths():
    try:
        raise ValueError("x")
    except ValueError as e:
        fr = frames(e.__traceback__)
    assert fr == [f"test_telemetry.py:test_frames_have_no_filesystem_paths:{fr[0].rsplit(':', 1)[1]}"]


def test_flush_sends_once_and_resets():
    t, sent = make()
    t.track("doc_pr_opened", user="u", files=3)
    assert asyncio.run(t.flush())
    assert len(sent) == 1
    url, key, batch = sent[0]
    assert key == "ingest" and batch["instance"]["id"] == "inst1" and batch["events"][0]["name"] == "doc_pr_opened"
    assert asyncio.run(t.flush()) and len(sent) == 1  # nothing new, nothing sent
    assert t.last_batch is batch


def test_disabled_sends_nothing_and_keeps_nothing():
    t, sent = make(enabled=False)
    t.track("login", user="u")
    asyncio.run(t.flush())
    assert sent == [] and not t.events


def test_failed_send_keeps_data_for_retry():
    t, _ = make()

    async def boom(*_):
        raise RuntimeError("collector down at https://collector.example")

    t._send = boom
    t.track("login")
    assert not asyncio.run(t.flush())
    assert len(t.events) == 1 and "<url>" in t.last_error


def test_no_url_means_no_collection():
    t, _ = make(url="")
    t.track("login")
    t.record_request("/x", "GET", 200, 1)
    assert not t.events and not t.requests


def test_bucket():
    assert [bucket(n) for n in (0, 1, 3, 40, 5000)] == ["0", "1", "<=5", "<=50", ">1000"]
