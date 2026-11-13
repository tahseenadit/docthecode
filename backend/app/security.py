"""Token encryption and signed session cookies."""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import get_settings

SESSION_COOKIE = "docthecode_session"
SESSION_MAX_AGE = 14 * 24 * 3600


def _fernet() -> Fernet:
    key = hashlib.sha256(("tokens:" + get_settings().secret_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(value: str | None) -> str | None:
    return None if value is None else _fernet().encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return None


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt=salt)


def make_session(user_id: str) -> str:
    return _serializer("session").dumps({"uid": user_id})


def read_session(cookie: str | None) -> str | None:
    if not cookie:
        return None
    try:
        return _serializer("session").loads(cookie, max_age=SESSION_MAX_AGE)["uid"]
    except (BadSignature, SignatureExpired, KeyError):
        return None


def make_state(data: dict) -> str:
    return _serializer("oauth-state").dumps(data)


def read_state(state: str, max_age: int = 600) -> dict | None:
    try:
        return _serializer("oauth-state").loads(state, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
