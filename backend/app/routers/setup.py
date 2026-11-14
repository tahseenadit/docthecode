"""First-run setup and admin settings.

Who may change settings:
  * before anyone is admin: whoever has the setup token from the server log
    (exchanged for a short-lived `docthecode_setup` cookie);
  * afterwards: DocTheCode admins. The person who completes setup and signs in first
    becomes admin, and admins can promote others.

GitHub App credentials can be created in one click with GitHub's App Manifest flow:
DocTheCode sends GitHub a manifest with the right permissions, webhook and callback URLs,
GitHub creates the app and hands back its ID, secrets and private key, and DocTheCode
stores them encrypted. No copy-pasting of secrets.
"""
from __future__ import annotations

import json
import re
import secrets

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import VERSION, default_api_url, get_settings
from ..services.backup import storage_status
from ..urls import is_secure, learn_from, public_url, set_public_url
from ..services.sync import LAST_WEBHOOK
from ..db import get_db
from ..models import GitHubHostConfig, RepoLink, User
from ..security import SESSION_COOKIE, make_state, read_session, read_state
from ..services import telemetry
from ..agent.copilot_agent import copilot_ping
from ..agent.providers import PROVIDERS, AgentConfig, ProviderError, list_models, messages_api
from ..github.client import GitHubError
from ..github.registry import github_client
from ..services.settings import (AGENT_BASE_URL, AGENT_MODEL, AGENT_PROVIDER, ANTHROPIC_KEY, agent_config,
                                 agent_key_setting, check_setup_token, get_setting, setup_token_required,
                                 host_admin, list_hosts, mask, needs_setup, set_setting, upsert_host)

router = APIRouter(tags=["setup"])
SETUP_COOKIE = "docthecode_setup"
HOST_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


# ------------------------------------------------------------------ authorization
def setup_cookie_valid(request: Request) -> bool:
    data = read_state(request.cookies.get(SETUP_COOKIE, ""), max_age=3 * 3600)
    return bool(data and data.get("setup"))


def configurator(request: Request, db: Session = Depends(get_db)) -> User | None:
    """Admin user, or (only while DocTheCode has no admin) the holder of the setup cookie."""
    uid = read_session(request.cookies.get(SESSION_COOKIE))
    user = db.get(User, uid) if uid else None
    if user and user.is_admin:
        return user
    if needs_setup(db) and setup_cookie_valid(request):
        return None
    raise HTTPException(403, "Only DocTheCode admins can change settings")


def webhook_url(host_id: str) -> str:
    return f"{public_url()}/api/webhooks/github/{host_id}"


def _is_local(url: str) -> bool:
    return bool(re.match(r"^https?://(localhost|127\.|0\.0\.0\.0|\[::1\])", url))


# ------------------------------------------------------------------ status & unlock
@router.get("/api/setup/status")
def status(request: Request, db: Session = Depends(get_db)):
    # While DocTheCode isn't set up, the address the admin is using becomes DocTheCode's public URL.
    base = learn_from(request, force=needs_setup(db))
    unlocked = needs_setup(db) and (setup_cookie_valid(request) or not setup_token_required())
    resp = JSONResponse({
        "needs_setup": needs_setup(db),
        "unlocked": unlocked,
        "hosts": [{"id": h.id, "name": h.name, "web_url": h.web_url} for h in list_hosts(db)],
        "agent_configured": agent_config(db).problem() is None,
        "version": VERSION,
        "public_url": base,
        "public_url_is_local": _is_local(base),
        "callback_url": f"{base}/api/auth/callback",
        "token_required": setup_token_required(),
        "telemetry_active": bool(get_settings().telemetry_url) and get_settings().telemetry_enabled,
        "storage": storage_status(),
    })
    if unlocked and not setup_cookie_valid(request):
        # No token configured: the person going through the wizard gets the setup pass, and becomes
        # admin when they sign in at the end.
        _grant_setup_cookie(resp)
    return resp


def _grant_setup_cookie(resp) -> None:
    resp.set_cookie(SETUP_COOKIE, make_state({"setup": True}), max_age=3 * 3600, httponly=True, samesite="lax",
                    secure=is_secure())


class Unlock(BaseModel):
    token: str


@router.post("/api/setup/unlock")
def unlock(body: Unlock, db: Session = Depends(get_db)):
    if not needs_setup(db):
        raise HTTPException(409, "DocTheCode is already set up. Sign in and ask an admin for access.")
    if not check_setup_token(db, body.token.strip()):
        raise HTTPException(403, "That setup token is not valid. Use the DOCTHECODE_SETUP_TOKEN value set on the server.")
    telemetry.track("setup_unlocked")
    resp = JSONResponse({"ok": True})
    _grant_setup_cookie(resp)
    return resp


# ------------------------------------------------------------------ GitHub App via manifest
class ManifestRequest(BaseModel):
    host_id: str = "github"
    host_name: str = "GitHub"
    web_url: str = "https://github.com"
    api_url: str | None = None
    organization: str = ""          # create the app under an org instead of your personal account
    app_name: str = Field(default="DocTheCode", max_length=34)
    webhook_url: str | None = None  # override the webhook address
    webhooks: bool = True           # False when GitHub can't reach DocTheCode: users click "Check GitHub for changes"
    public: bool = False            # allow installing on other accounts/orgs


@router.post("/api/setup/github-app/manifest")
def start_manifest(body: ManifestRequest, _=Depends(configurator)):
    base = public_url()
    if not HOST_ID_RE.match(body.host_id):
        raise HTTPException(422, "Host id: lowercase letters, digits and dashes")
    hook = (body.webhook_url or webhook_url(body.host_id)).strip()
    if body.webhooks and _is_local(hook):
        raise HTTPException(422, "GitHub cannot deliver webhooks to localhost. Turn webhooks off and use "
                                 "'Check GitHub for changes' instead.")
    web = body.web_url.rstrip("/")
    manifest = {
        "name": body.app_name,
        "url": base,
        **({"hook_attributes": {"url": hook, "active": True}} if body.webhooks else {}),
        "redirect_url": f"{base}/api/setup/github-app/callback",
        "callback_urls": [f"{base}/api/auth/callback"],
        "setup_url": f"{base}/setup",
        "public": body.public,
        "default_permissions": {
            "checks": "write", "pull_requests": "write", "contents": "read", "metadata": "read",
            # No account permissions (e.g. email_addresses): GitHub rejects them in a manifest
            # ("Default permission records resource is not included in the list").
            "administration": "write",
            "members": "read",  # org teams, so team-based access is inherited correctly
        },
        "default_events": ["pull_request", "check_run", "member"] if body.webhooks else [],
    }
    state = make_state({"manifest": True, "host_id": body.host_id, "host_name": body.host_name, "web_url": web,
                        "api_url": (body.api_url or default_api_url(web)).rstrip("/")})
    path = f"/organizations/{body.organization}/settings/apps/new" if body.organization else "/settings/apps/new"
    return {"action": f"{web}{path}?state={state}", "manifest": json.dumps(manifest)}


def credentials_from_conversion(app: dict) -> dict:
    """Map GitHub's manifest-conversion response to a host's credentials.

    GitHub returns no webhook secret when the app was created without a webhook (DocTheCode on a private
    address). DocTheCode then never receives webhooks, but stores a random secret so the host is complete
    and a webhook can be switched on later by entering the same secret on GitHub.
    """
    return {"app_id": str(app["id"]), "app_slug": app["slug"], "client_id": app["client_id"],
            "client_secret": app["client_secret"], "private_key": app["pem"],
            "webhook_secret": app.get("webhook_secret") or secrets.token_hex(32)}


@router.get("/api/setup/github-app/callback")
async def manifest_callback(request: Request, code: str, state: str, db: Session = Depends(get_db)):
    configurator(request, db)
    data = read_state(state, max_age=3600)
    if not data or not data.get("manifest"):
        raise HTTPException(400, "This setup link expired. Start the GitHub App creation again.")
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(f"{data['api_url']}/app-manifests/{code}/conversions",
                            headers={"Accept": "application/vnd.github+json"})
    if r.status_code >= 400:
        raise HTTPException(502, f"GitHub did not return the app credentials: {r.text}")
    try:
        creds = credentials_from_conversion(r.json())
        upsert_host(db, data["host_id"], name=data["host_name"], web_url=data["web_url"], api_url=data["api_url"],
                    **creds)
    except (KeyError, ValueError) as e:
        raise HTTPException(502, f"GitHub created the app but its credentials were incomplete ({e}). Delete the app "
                                 "on GitHub and try again, or enter it under “I already have an app”.")
    db.commit()
    telemetry.track("github_app_saved", method="manifest", enterprise=data["web_url"] != "https://github.com")
    return RedirectResponse(f"{public_url()}/setup?created={data['host_id']}")


# ------------------------------------------------------------------ manual host entry / edit
class HostIn(BaseModel):
    id: str
    name: str
    web_url: str = "https://github.com"
    api_url: str | None = None
    app_id: str
    app_slug: str
    client_id: str
    client_secret: str | None = None   # leave empty to keep the stored value
    private_key: str | None = None
    webhook_secret: str | None = None


@router.put("/api/admin/hosts/{host_id}")
def save_host(host_id: str, body: HostIn, db: Session = Depends(get_db), _=Depends(configurator)):
    if host_id != body.id or not HOST_ID_RE.match(host_id):
        raise HTTPException(422, "Host id: lowercase letters, digits and dashes")
    if body.private_key and "PRIVATE KEY" not in body.private_key:
        raise HTTPException(422, "The private key should be the contents of the .pem file GitHub gave you")
    try:
        row = upsert_host(db, host_id, name=body.name, web_url=body.web_url,
                          api_url=body.api_url or default_api_url(body.web_url), app_id=body.app_id,
                          app_slug=body.app_slug, client_id=body.client_id, client_secret=body.client_secret,
                          private_key=body.private_key, webhook_secret=body.webhook_secret)
    except ValueError as e:
        raise HTTPException(422, str(e))
    db.commit()
    telemetry.track("github_app_saved", method="manual", enterprise=row.web_url != "https://github.com")
    return host_admin(row)


@router.delete("/api/admin/hosts/{host_id}", status_code=204)
def delete_host(host_id: str, db: Session = Depends(get_db), _=Depends(configurator)):
    if db.scalar(select(RepoLink.id).where(RepoLink.host_id == host_id).limit(1)):
        raise HTTPException(409, "Disconnect the repositories that use this host first")
    row = db.get(GitHubHostConfig, host_id)
    if row:
        db.delete(row)
        db.commit()


# ------------------------------------------------------------------ documentation agent (AI provider)
class AgentIn(BaseModel):
    provider: str
    api_key: str | None = None   # empty keeps the stored key for this provider
    model: str | None = None
    base_url: str | None = None


def _agent(db: Session) -> dict:
    cfg = agent_config(db)
    return {
        "provider": cfg.provider, "model": cfg.model, "effective_model": cfg.effective_model,
        "base_url": cfg.base_url, "api_key": mask(cfg.api_key), "problem": cfg.problem(),
        "stored_keys": {p: bool(get_setting(db, agent_key_setting(p))) for p in PROVIDERS},
        "providers": [{"id": p.id, "label": p.label, "needs_key": p.needs_key, "default_model": p.default_model,
                       "base_url": p.base_url, "key_hint": p.key_hint, "key_url": p.key_url} for p in PROVIDERS.values()],
    }


def _candidate(db: Session, body: AgentIn) -> AgentConfig:
    """The configuration being edited: the submitted values, falling back to what's stored."""
    if body.provider not in PROVIDERS:
        raise HTTPException(422, "Unknown provider")
    stored = get_setting(db, agent_key_setting(body.provider))
    if not stored and body.provider == "anthropic":
        stored = get_setting(db, ANTHROPIC_KEY)
    return AgentConfig(body.provider, (body.api_key or "").strip() or stored, (body.model or "").strip() or None,
                       (body.base_url or "").strip().rstrip("/") or None)


@router.put("/api/admin/agent")
def save_agent(body: AgentIn, db: Session = Depends(get_db), _=Depends(configurator)):
    cfg = _candidate(db, body)
    set_setting(db, AGENT_PROVIDER, cfg.provider)
    if body.api_key and body.api_key.strip():
        set_setting(db, agent_key_setting(cfg.provider), body.api_key.strip(), secret=True)
    set_setting(db, AGENT_MODEL, cfg.model)
    set_setting(db, AGENT_BASE_URL, cfg.base_url)
    db.commit()
    telemetry.track("agent_configured", provider=cfg.provider, model=cfg.effective_model,
                    custom_base_url=bool(cfg.base_url))
    return _agent(db)


@router.post("/api/admin/agent/models")
async def agent_models(body: AgentIn, db: Session = Depends(get_db), _=Depends(configurator)):
    cfg = _candidate(db, body)
    try:
        return {"models": await list_models(cfg)}
    except ProviderError as e:
        raise HTTPException(502, f"Could not list models: {e}")


async def _any_installation_token(db: Session) -> str:
    """A GitHub App installation token for an organization, to try Copilot before any repo is connected."""
    link = db.scalar(select(RepoLink).limit(1))
    if link:
        return await github_client(link.host_id).installation_token(link.installation_id)
    for host in list_hosts(db):
        gh = github_client(host.id)
        for inst in await gh.app_installations():
            if inst.get("target_type") == "Organization":
                return await gh.installation_token(inst["id"])
    raise HTTPException(409, "Install DocTheCode's GitHub App on an organization first: Copilot is billed to an organization.")


@router.post("/api/admin/agent/test")
async def agent_test(body: AgentIn, db: Session = Depends(get_db), _=Depends(configurator)):
    """Checks the key, model and tool calling with a one-tool round trip."""
    cfg = _candidate(db, body)
    if cfg.problem():
        raise HTTPException(422, cfg.problem())
    try:
        if cfg.provider == "copilot":
            await copilot_ping(await _any_installation_token(db), cfg.effective_model or "auto")
        else:
            resp = await messages_api(cfg).create(
                model=cfg.effective_model, max_tokens=300, system="You are testing a tool integration.",
                tools=[{"name": "ping", "description": "Reply to a connectivity check.",
                        "input_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}}}],
                messages=[{"role": "user", "content": "Call the ping tool once with ok=true."}])
            if not any(b.get("type") == "tool_use" for b in resp.content):
                raise ProviderError("The model answered but did not call the tool. Pick a model that supports "
                                    "tool (function) calling.")
    except (ProviderError, GitHubError) as e:
        telemetry.track("agent_test", provider=cfg.provider, ok=False)
        raise HTTPException(502, str(e))
    telemetry.track("agent_test", provider=cfg.provider, ok=True)
    return {"ok": True, "model": cfg.effective_model}


# ------------------------------------------------------------------ admin overview & admins
@router.get("/api/admin/config")
def config(db: Session = Depends(get_db), _=Depends(configurator)):
    return {
        "hosts": [{**host_admin(h), "webhook_url": webhook_url(h.id)} for h in list_hosts(db)],
        "agent": _agent(db),
        "admins": [{"handle": u.handle, "avatar_url": u.avatar_url}
                   for u in db.scalars(select(User).where(User.is_admin.is_(True)))],
        "callback_url": f"{public_url()}/api/auth/callback",
        "public_url": public_url(),
        "public_url_locked": bool(get_settings().public_url_override),
        "github_events": {
            "last_webhook_at": {h.id: LAST_WEBHOOK.get(h.id) for h in list_hosts(db)},
            "links": [{"full_name": l.gh_full_name, "checked_at": l.polled_at, "check_error": l.poll_error}
                      for l in db.scalars(select(RepoLink).where(RepoLink.enabled.is_(True)))],
        },
        "storage": storage_status(),
    }


class PublicUrlIn(BaseModel):
    url: str = Field(pattern=r"^https?://[^/\s]+$")


@router.put("/api/admin/public-url")
def save_public_url(body: PublicUrlIn, _=Depends(configurator)):
    """After moving DocTheCode (e.g. to a custom domain). Update the GitHub App's URLs to match."""
    if get_settings().public_url_override:
        raise HTTPException(409, "DOCTHECODE_PUBLIC_URL is set on the server, so the address can't be changed here")
    set_public_url(body.url)
    return {"public_url": public_url()}


class AdminIn(BaseModel):
    handle: str


@router.post("/api/admin/admins", status_code=201)
def add_admin(body: AdminIn, db: Session = Depends(get_db), _=Depends(configurator)):
    login, _, host = body.handle.strip().partition("@")
    user = db.scalar(select(User).where(User.login == login, User.host_id == (host or "github")))
    if not user:
        raise HTTPException(404, "That person has not signed in to DocTheCode yet")
    user.is_admin = True
    db.commit()
    telemetry.track("admin_added")
    return {"handle": user.handle}


@router.delete("/api/admin/admins/{handle}", status_code=204)
def remove_admin(handle: str, db: Session = Depends(get_db), me: User | None = Depends(configurator)):
    login, _, host = handle.partition("@")
    user = db.scalar(select(User).where(User.login == login, User.host_id == (host or "github")))
    if user and me and user.id == me.id:
        raise HTTPException(409, "You cannot remove yourself; ask another admin")
    if user:
        user.is_admin = False
        db.commit()
