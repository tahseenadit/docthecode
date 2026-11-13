"""DocTheCode's public address, learned instead of configured.

GitHub needs absolute URLs (OAuth callback, webhook URL, links in check runs). Rather than
asking for an environment variable, DocTheCode records the address the admin uses during setup
(the browser's scheme + host, as seen through the hosting platform's proxy) and stores it in
the database. Admins can change it later (e.g. after adding a custom domain).
DOCTHECODE_PUBLIC_URL, if set, always wins.
"""
from __future__ import annotations

import threading

from fastapi import Request

from .config import get_settings

SETTING_KEY = "public_url"
FALLBACK = "http://localhost:5173"
_cache: dict[str, str] = {}
_lock = threading.Lock()


def _stored() -> str | None:
    from .db import SessionLocal
    from .services.settings import get_setting

    with SessionLocal() as db:
        return get_setting(db, SETTING_KEY)


def public_url() -> str:
    override = get_settings().public_url_override
    if override:
        return override
    with _lock:
        if "url" in _cache:
            return _cache["url"]
    url = _stored()
    if url:
        with _lock:
            _cache["url"] = url
        return url
    return FALLBACK


def from_request(request: Request) -> str:
    """The address this request was made to: https://docthecode-xyz.a.run.app, http://localhost:5173, ..."""
    return str(request.base_url).rstrip("/")


def set_public_url(url: str) -> None:
    from .db import SessionLocal
    from .services.settings import set_setting

    url = url.strip().rstrip("/")
    with SessionLocal() as db:
        set_setting(db, SETTING_KEY, url)
        db.commit()
    with _lock:
        _cache["url"] = url


def learn_from(request: Request, force: bool = False) -> str:
    """Remember the address the browser used, if none is known yet (or `force`, during setup)."""
    if get_settings().public_url_override:
        return public_url()
    seen = from_request(request)
    if force or not _stored():
        if seen != _cache.get("url"):
            set_public_url(seen)
    return public_url()


def is_secure() -> bool:
    return public_url().startswith("https://")
