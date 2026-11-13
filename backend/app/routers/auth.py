from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..urls import learn_from, public_url
from ..db import get_db
from ..deps import current_user
from ..github.client import GitHubError
from ..github.registry import github_client
from ..models import GitHubHostConfig, User
from ..security import (SESSION_COOKIE, SESSION_MAX_AGE, encrypt, make_session, make_state, read_state)
from ..services.access import sync_user_access
from ..services.settings import host_public, list_hosts, needs_setup
from .setup import SETUP_COOKIE, setup_cookie_valid
from ..services import telemetry

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/hosts")
def hosts(db: Session = Depends(get_db)):
    """GitHub hosts users can sign in with (github.com and/or enterprise servers)."""
    return [host_public(h) for h in list_hosts(db)]


@router.get("/login/{host_id}")
def login(host_id: str, request: Request, next: str = "/"):
    base = learn_from(request)
    try:
        gh = github_client(host_id)
    except KeyError:
        raise HTTPException(404, "Unknown GitHub host")
    state = make_state({"host": host_id, "next": next if next.startswith("/") else "/",
                        "nonce": secrets.token_urlsafe(8)})
    resp = RedirectResponse(gh.authorize_url(f"{base}/api/auth/callback", state))
    resp.set_cookie("docthecode_oauth_state", state, max_age=600, httponly=True, samesite="lax",
                    secure=base.startswith("https"))
    return resp


@router.get("/callback")
async def callback(request: Request, state: str, code: str | None = None, error: str | None = None,
                   db: Session = Depends(get_db)):
    data = read_state(state)
    base = public_url()
    if error:
        return RedirectResponse(f"{base}/?login_error={error}")
    if not data or request.cookies.get("docthecode_oauth_state") != state or not code:
        raise HTTPException(400, "Invalid or expired login state. Go back and sign in again.")
    gh = github_client(data["host"])
    try:
        tok = await gh.exchange_code(code, f"{base}/api/auth/callback")
        profile = await gh.current_user(tok.access_token)
    except GitHubError as e:
        raise HTTPException(400, f"GitHub login failed: {e}")

    user = db.scalar(select(User).where(User.host_id == data["host"], User.github_id == profile["id"]))
    first_login = user is None
    if not user:
        user = User(host_id=data["host"], github_id=profile["id"])
        db.add(user)
    user.login = profile["login"]
    user.name = profile.get("name")
    user.avatar_url = profile.get("avatar_url")
    user.email = profile.get("email") or await gh.user_primary_email(tok.access_token)
    user.access_token_enc = encrypt(tok.access_token)
    user.refresh_token_enc = encrypt(tok.refresh_token)
    user.token_expires_at = tok.expires_at
    # The person who ran setup (holds the setup cookie) becomes the first admin.
    if needs_setup(db) and setup_cookie_valid(request):
        user.is_admin = True
    db.commit()

    telemetry.track("login", user, first_login=first_login, admin=user.is_admin,
                    enterprise=gh.host.web_url != "https://github.com")
    try:  # find every connected doc repo this user should see
        await sync_user_access(db, user)
    except GitHubError as e:
        log.warning("access sync after login failed for %s: %s", user.login, e)
        db.rollback()

    resp = RedirectResponse(f"{base}{data['next']}")
    resp.set_cookie(SESSION_COOKIE, make_session(user.id), max_age=SESSION_MAX_AGE, httponly=True,
                    samesite="lax", secure=base.startswith("https"))
    resp.delete_cookie("docthecode_oauth_state")
    if user.is_admin:
        resp.delete_cookie(SETUP_COOKIE)
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@router.get("/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    host = db.get(GitHubHostConfig, user.host_id)
    return {"id": user.id, "login": user.login, "handle": user.handle, "name": user.name,
            "avatar_url": user.avatar_url, "is_admin": user.is_admin,
            "host": host_public(host) if host else {"id": user.host_id, "name": user.host_id, "web_url": ""}}
