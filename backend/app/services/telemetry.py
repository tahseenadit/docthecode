"""Anonymous product telemetry: usage events, request stats and errors.

Sent in small batches to the DocTheCode vendor's collector (DOCTHECODE_TELEMETRY_URL), which stores
them in the vendor's online database. Organization data never leaves the organization:

  Sent                                          Never sent
  ------------------------------------------    ---------------------------------------------
  random instance id, DocTheCode version, OS,       doc contents, code, diffs, file or page names,
  database kind, counts (users, repos, PRs)     repo/org/user names, emails, GitHub logins,
  event names with small numeric/enum props     tokens, keys, URLs, review text, agent prompts
  per-user ids hashed with this server's
  secret key (not reversible by the vendor)
  route templates, status codes, timings        actual request paths or bodies
  error type, scrubbed message, code frames     local variables, request data

Admins can switch it off under Admin → Usage data; DOCTHECODE_TELEMETRY=off disables it for the
whole server. The Admin page also shows the last batch that was sent.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import platform
import re
import sys
import time
import traceback
import uuid
from collections import deque
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

MAX_EVENTS = 2000
MAX_ERRORS = 300
FLUSH_INTERVAL = 60
HEARTBEAT_INTERVAL = 6 * 3600
_STR_PROP_MAX = 64

_SCRUBBERS = [
    (re.compile(r"(gh[opsur]_|github_pat_)[A-Za-z0-9_]{10,}"), "<github-token>"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"), "<anthropic-key>"),
    (re.compile(r"-----BEGIN [A-Z ]+-----[\s\S]*?-----END [A-Z ]+-----"), "<pem>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"(?<![\w/])/(?:[\w.\-]+/)+[\w.\-]*"), "<path>"),
    (re.compile(r"\b[0-9a-f]{20,}\b", re.I), "<hex>"),
    (re.compile(r"'[^']{24,}'|\"[^\"]{24,}\""), "<text>"),
]


def scrub(text: str, limit: int = 300) -> str:
    """Remove anything that could identify an organization or leak data from an error message."""
    text = str(text)
    for pattern, repl in _SCRUBBERS:
        text = pattern.sub(repl, text)
    return text[:limit]


def clean_props(props: dict[str, Any]) -> dict[str, Any]:
    """Only flat scalars; short strings only (enums like "approved", never free text)."""
    out: dict[str, Any] = {}
    for k, v in list(props.items())[:20]:
        key = re.sub(r"[^a-z0-9_]", "_", str(k).lower())[:40]
        if isinstance(v, bool) or v is None or isinstance(v, (int, float)):
            out[key] = v
        elif isinstance(v, str) and len(v) <= _STR_PROP_MAX and re.fullmatch(r"[\w .:/\-]*", v):
            out[key] = v
    return out


def frames(tb, limit: int = 12) -> list[str]:
    """`module:function:line` for each frame. No file system paths, no source, no locals."""
    out = []
    for fs in traceback.extract_tb(tb)[-limit:]:
        f = fs.filename.replace("\\", "/")
        if "/app/" in f:
            mod = "app/" + f.split("/app/", 1)[1]
        elif "site-packages/" in f:
            mod = f.split("site-packages/", 1)[1].split("/", 1)[0]
        else:
            mod = f.rsplit("/", 1)[-1]
        out.append(f"{mod}:{fs.name}:{fs.lineno}")
    return out


def fingerprint(kind: str, stack: list[str]) -> str:
    own = [s.rsplit(":", 1)[0] for s in stack if s.startswith("app/")] or stack[-3:]
    return hashlib.sha1((kind + "|" + "|".join(own[-4:])).encode()).hexdigest()[:16]


def bucket(n: int) -> str:
    """Coarse counts so small installs can't be singled out."""
    for edge in (0, 1, 5, 10, 25, 50, 100, 250, 500, 1000):
        if n <= edge:
            return str(edge) if edge in (0, 1) else f"<={edge}"
    return ">1000"


class Telemetry:
    def __init__(self, *, url: str, key: str, secret: str, version: str,
                 enabled: Callable[[], bool], instance: Callable[[], dict],
                 send: Callable[[str, str, dict], Awaitable[None]] | None = None):
        self.url, self.key, self.secret, self.version = url, key, secret, version
        self._enabled, self._instance, self._send = enabled, instance, send or _http_send
        self.events: deque[dict] = deque(maxlen=MAX_EVENTS)
        self.errors: deque[dict] = deque(maxlen=MAX_ERRORS)
        self.requests: dict[tuple[str, str, int], list[float]] = {}  # (route, method, status) -> [count, total, max]
        self.window_start = time.time()
        self.last_batch: dict | None = None
        self.last_sent_at: float | None = None
        self.last_error: str | None = None
        self.dropped = 0

    # ---------------------------------------------------------------- recording
    @property
    def active(self) -> bool:
        try:
            return bool(self.url) and self._enabled()
        except Exception:
            return False

    def user_hash(self, user_id: str | None) -> str | None:
        if not user_id:
            return None
        return hmac.new(self.secret.encode(), b"telemetry:" + user_id.encode(), hashlib.sha256).hexdigest()[:16]

    def track(self, name: str, user: Any = None, **props: Any) -> None:
        if not self.url:
            return
        self.events.append({"name": re.sub(r"[^a-z0-9_.]", "_", name.lower())[:60], "ts": time.time(),
                            "user": self.user_hash(getattr(user, "id", user)), "props": clean_props(props)})

    def record_request(self, route: str, method: str, status: int, ms: float) -> None:
        if not self.url:
            return
        k = (route[:120], method, status)
        s = self.requests.get(k)
        if s is None:
            self.requests[k] = [1, ms, ms]
        else:
            s[0] += 1
            s[1] += ms
            s[2] = max(s[2], ms)

    def record_exception(self, exc: BaseException, source: str, route: str | None = None, **props: Any) -> None:
        self.record_error(source=source, kind=type(exc).__name__, message=str(exc),
                          stack=frames(exc.__traceback__), route=route, **props)

    def record_error(self, *, source: str, kind: str, message: str, stack: list[str] | None = None,
                     route: str | None = None, **props: Any) -> None:
        if not self.url:
            return
        stack = [scrub(s, 120) for s in (stack or [])][-15:]
        kind = re.sub(r"[^\w.]", "", kind)[:80] or "Error"
        self.errors.append({"ts": time.time(), "source": source[:20], "kind": kind, "message": scrub(message),
                            "stack": stack, "route": (route or "")[:120], "fingerprint": fingerprint(kind, stack),
                            "props": clean_props(props)})

    # ---------------------------------------------------------------- sending
    def build_batch(self) -> dict | None:
        now = time.time()
        stats = [{"route": r, "method": m, "status": st, "count": int(c), "total_ms": round(t, 1), "max_ms": round(mx, 1)}
                 for (r, m, st), (c, t, mx) in self.requests.items()]
        if not (self.events or self.errors or stats):
            return None
        batch = {
            "schema": 1, "sent_at": now,
            "instance": {**self._instance(), "version": self.version, "python": platform.python_version(),
                         "os": sys.platform},
            "events": list(self.events), "errors": list(self.errors),
            "request_stats": stats, "window": {"start": self.window_start, "end": now},
        }
        return batch

    def _reset(self) -> None:
        self.events.clear()
        self.errors.clear()
        self.requests.clear()
        self.window_start = time.time()

    async def flush(self) -> bool:
        if not self.active:
            self._reset()  # disabled: keep nothing
            return False
        batch = self.build_batch()
        if not batch:
            return True
        try:
            await self._send(self.url, self.key, batch)
        except Exception as e:  # keep the data for the next try; queues are bounded
            self.last_error = scrub(str(e), 200)
            return False
        self.last_batch, self.last_sent_at, self.last_error = batch, time.time(), None
        self._reset()
        return True


async def _http_send(url: str, key: str, batch: dict) -> None:
    import httpx

    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.post(f"{url}/v1/batch", json=batch, headers={"X-DocTheCode-Key": key, "User-Agent": "DocTheCode"})
        r.raise_for_status()


# ------------------------------------------------------------------ app wiring
_client: Telemetry | None = None
_enabled_cache: tuple[float, bool] = (0.0, True)
SETTING_KEY = "telemetry"
INSTANCE_KEY = "instance_id"


def _admin_enabled() -> bool:
    """Admin toggle (cached for a minute)."""
    global _enabled_cache
    if time.time() - _enabled_cache[0] < 60:
        return _enabled_cache[1]
    from ..db import SessionLocal
    from .settings import get_setting

    with SessionLocal() as db:
        value = (get_setting(db, SETTING_KEY) or "on") != "off"
    _enabled_cache = (time.time(), value)
    return value


def invalidate_enabled_cache() -> None:
    global _enabled_cache
    _enabled_cache = (0.0, True)


def instance_id() -> str:
    from ..db import SessionLocal
    from .settings import get_setting, set_setting

    with SessionLocal() as db:
        iid = get_setting(db, INSTANCE_KEY)
        if not iid:
            iid = uuid.uuid4().hex
            set_setting(db, INSTANCE_KEY, iid)
            db.commit()
        return iid


def _instance_info() -> dict:
    from ..config import get_settings

    s = get_settings()
    return {"id": instance_id(), "db": "sqlite" if s.database_url.startswith("sqlite") else "postgres",
            "storage": s.repo_storage}


def client() -> Telemetry:
    global _client
    if _client is None:
        from ..config import VERSION, get_settings

        s = get_settings()
        _client = Telemetry(url=s.telemetry_url if s.telemetry_enabled else "", key=s.telemetry_key,
                            secret=s.secret_key, version=VERSION, enabled=_admin_enabled, instance=_instance_info)
    return _client


def track(name: str, user: Any = None, **props: Any) -> None:
    try:
        client().track(name, user, **props)
    except Exception:  # telemetry must never break the product
        pass


def record_exception(exc: BaseException, source: str, route: str | None = None, **props: Any) -> None:
    try:
        client().record_exception(exc, source, route, **props)
    except Exception:
        pass


def heartbeat() -> None:
    """Coarse install-size numbers, a few times a day."""
    from sqlalchemy import func, select

    from ..db import SessionLocal
    from ..models import DocPullRequest, DocRepo, GitHubHostConfig, RepoLink, User

    with SessionLocal() as db:
        count = lambda q: db.scalar(select(func.count()).select_from(q)) or 0  # noqa: E731
        hosts = list(db.scalars(select(GitHubHostConfig.web_url)))
        track("instance_heartbeat",
              users=bucket(count(User)), active_users_7d=bucket(db.scalar(
                  select(func.count()).select_from(User).where(User.access_synced_at > time.time() - 7 * 86400)) or 0),
              doc_repos=bucket(count(DocRepo)), connected_repos=bucket(count(RepoLink)),
              open_doc_prs=bucket(db.scalar(select(func.count()).select_from(DocPullRequest)
                                            .where(DocPullRequest.state == "open")) or 0),
              github_com=any(h == "https://github.com" for h in hosts),
              enterprise_hosts=sum(1 for h in hosts if h != "https://github.com"))


async def run_forever() -> None:
    last_heartbeat = 0.0
    while True:
        try:
            if client().active and time.time() - last_heartbeat > HEARTBEAT_INTERVAL:
                heartbeat()
                last_heartbeat = time.time()
            await client().flush()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(FLUSH_INTERVAL)


class ErrorLogHandler(logging.Handler):
    """Forward ERROR logs that carry an exception (log.exception(...)) as telemetry errors."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(__name__) or not record.exc_info or not record.exc_info[1]:
            return
        record_exception(record.exc_info[1], source="log", logger=record.name[:60])


WHAT_IS_SENT = [
    "A random ID for this DocTheCode server, its version, OS and database kind",
    "Rounded counts: users, doc repos, connected repos, open doc PRs",
    "Feature usage events (e.g. doc PR opened, review approved, agent run finished with duration)",
    "Per-user IDs hashed with this server's secret key, so DocTheCode's developers can't tell who anyone is",
    "API route templates (like /api/repos/{owner}/{name}), status codes and response times",
    "Errors: type, a scrubbed message and code locations (no paths, values or data)",
]
NEVER_SENT = [
    "Documentation or code contents, diffs, file or page names",
    "Names of repositories, organizations or users; emails; GitHub logins",
    "Tokens, keys, URLs, review comments, agent prompts or outputs",
]
