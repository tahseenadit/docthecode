"""Credentials and settings stored in the database (encrypted with DOCTHECODE_SECRET_KEY)."""
from __future__ import annotations

import hashlib
import hmac
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import GitHubHost
from ..db import SessionLocal
from ..models import AppSetting, GitHubHostConfig, User
from ..security import decrypt, encrypt

log = logging.getLogger(__name__)

ANTHROPIC_KEY = "anthropic_api_key"  # legacy
AGENT_PROVIDER = "agent_provider"
AGENT_API_KEY = "agent_api_key"
AGENT_BASE_URL = "agent_base_url"
AGENT_MODEL = "agent_model"
SETUP_TOKEN_HASH = "setup_token_hash"


# ------------------------------------------------------------------ key/value settings
def get_setting(db: Session, key: str, default: str | None = None) -> str | None:
    row = db.get(AppSetting, key)
    if not row:
        return default
    return decrypt(row.value) if row.is_secret else row.value


def set_setting(db: Session, key: str, value: str | None, secret: bool = False) -> None:
    row = db.get(AppSetting, key)
    if value is None:
        if row:
            db.delete(row)
        return
    stored = encrypt(value) if secret else value
    if row:
        row.value, row.is_secret = stored, secret
    else:
        db.add(AppSetting(key=key, value=stored, is_secret=secret))


def agent_key_setting(provider: str) -> str:
    return f"{AGENT_API_KEY}:{provider}"  # one stored key per provider, so switching back keeps it


def agent_config(db: Session | None = None):
    """The documentation agent's provider settings (API key decrypted, in memory only)."""
    from ..agent.providers import AgentConfig

    def read(db: Session):
        legacy_key = get_setting(db, ANTHROPIC_KEY)  # settings saved before providers were selectable
        provider = get_setting(db, AGENT_PROVIDER) or ("anthropic" if legacy_key else None)
        key = get_setting(db, agent_key_setting(provider)) if provider else None
        if not key and provider == "anthropic":
            key = legacy_key
        return AgentConfig(provider=provider, api_key=key, model=get_setting(db, AGENT_MODEL),
                           base_url=get_setting(db, AGENT_BASE_URL))

    if db is not None:
        return read(db)
    with SessionLocal() as own:
        return read(own)


def mask(secret: str | None) -> str | None:
    if not secret:
        return None
    return "••••" + secret[-4:] if len(secret) > 8 else "••••"


# ------------------------------------------------------------------ GitHub hosts
def to_host(row: GitHubHostConfig) -> GitHubHost:
    return GitHubHost(
        id=row.id, name=row.name, web_url=row.web_url, api_url=row.api_url, app_id=row.app_id,
        app_slug=row.app_slug, client_id=row.client_id, client_secret=decrypt(row.client_secret_enc) or "",
        private_key=decrypt(row.private_key_enc) or "", webhook_secret=decrypt(row.webhook_secret_enc) or "",
    )


def load_host(host_id: str) -> GitHubHost:
    with SessionLocal() as db:
        row = db.get(GitHubHostConfig, host_id)
        if not row:
            raise KeyError(f"Unknown GitHub host '{host_id}'")
        return to_host(row)


def host_version(host_id: str) -> str | None:
    with SessionLocal() as db:
        row = db.get(GitHubHostConfig, host_id)
        return row.updated_at.isoformat() if row else None


def list_hosts(db: Session) -> list[GitHubHostConfig]:
    return list(db.scalars(select(GitHubHostConfig).order_by(GitHubHostConfig.created_at)))


def host_public(row: GitHubHostConfig) -> dict:
    """Host info that is safe to show to any signed-in user."""
    return {"id": row.id, "name": row.name, "web_url": row.web_url}


def host_admin(row: GitHubHostConfig) -> dict:
    """Host info for admins: everything except the secrets themselves."""
    return {**host_public(row), "api_url": row.api_url, "app_id": row.app_id, "app_slug": row.app_slug,
            "client_id": row.client_id, "app_url": f"{row.web_url}/apps/{row.app_slug}",
            "client_secret": mask(decrypt(row.client_secret_enc)),
            "webhook_secret": mask(decrypt(row.webhook_secret_enc)),
            "private_key": "set" if row.private_key_enc else None,
            "updated_at": row.updated_at.isoformat()}


def upsert_host(db: Session, host_id: str, *, name: str, web_url: str, api_url: str, app_id: str, app_slug: str,
                client_id: str, client_secret: str | None, private_key: str | None,
                webhook_secret: str | None) -> GitHubHostConfig:
    """Create or update a host. Secret fields left as None keep their stored value."""
    row = db.get(GitHubHostConfig, host_id)
    if row is None:
        if not (client_secret and private_key and webhook_secret):
            raise ValueError("client secret, private key and webhook secret are required for a new host")
        row = GitHubHostConfig(id=host_id)
        db.add(row)
    row.name, row.web_url, row.api_url = name, web_url.rstrip("/"), api_url.rstrip("/")
    row.app_id, row.app_slug, row.client_id = str(app_id), app_slug, client_id
    if client_secret:
        row.client_secret_enc = encrypt(client_secret)
    if private_key:
        row.private_key_enc = encrypt(private_key.strip() + "\n")
    if webhook_secret:
        row.webhook_secret_enc = encrypt(webhook_secret)
    return row


# ------------------------------------------------------------------ first-run setup token
def needs_setup(db: Session) -> bool:
    """True until someone has signed in as the first admin."""
    return db.scalar(select(User.id).where(User.is_admin.is_(True)).limit(1)) is None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def setup_token_required() -> bool:
    """Only when the operator set DOCTHECODE_SETUP_TOKEN. Otherwise the first visitor runs setup."""
    from ..config import get_settings

    return bool(get_settings().setup_token)


def issue_setup_token() -> None:
    """Log that DocTheCode is waiting for setup (and store the required token's hash, if one is set).

    By default there is no token: the first person to open DocTheCode runs the setup wizard and becomes
    admin, like Gitea or Mattermost. Operators who expose DocTheCode publicly before finishing setup can
    set DOCTHECODE_SETUP_TOKEN, and then only someone who knows it can run setup.
    """
    from ..config import get_settings

    token = get_settings().setup_token
    with SessionLocal() as db:
        if not needs_setup(db):
            return
        set_setting(db, SETUP_TOKEN_HASH, _hash(token) if token else None)
        db.commit()
    if token:
        log.warning("DocTheCode is not set up yet. Open it in a browser and enter the DOCTHECODE_SETUP_TOKEN value.")
    else:
        log.warning("DocTheCode is not set up yet. Open it in a browser: the first person to do so runs the setup "
                    "and becomes the admin.")


def check_setup_token(db: Session, token: str | None) -> bool:
    if not token or not needs_setup(db):
        return False
    stored = get_setting(db, SETUP_TOKEN_HASH)
    return bool(stored) and hmac.compare_digest(stored, _hash(token))
