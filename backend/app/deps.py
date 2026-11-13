"""Request dependencies: current user, user tokens, and doc repo permissions."""
from __future__ import annotations

import time

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .github.registry import github_client
from .models import DocPullRequest, DocRepo, RepoMember, User
from .security import SESSION_COOKIE, decrypt, encrypt, read_session
from .services.access import access_by_repo, ensure_access_fresh
from .services.members import inherited_role

ROLE_RANK = {None: 0, "read": 1, "write": 2, "admin": 3}
_GH_TO_ROLE = {"admin": "admin", "maintain": "write", "write": "write", "triage": "read", "read": "read"}


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = read_session(request.cookies.get(SESSION_COOKIE))
    user = db.get(User, uid) if uid else None
    if not user:
        raise HTTPException(401, "Not signed in")
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Only DocTheCode admins can do this")
    return user


async def user_token(db: Session, user: User) -> str:
    """The user's GitHub token, refreshed if it has expired."""
    if user.token_expires_at and user.token_expires_at < time.time() + 60 and user.refresh_token_enc:
        tok = await github_client(user.host_id).refresh(decrypt(user.refresh_token_enc))
        user.access_token_enc = encrypt(tok.access_token)
        user.refresh_token_enc = encrypt(tok.refresh_token) if tok.refresh_token else user.refresh_token_enc
        user.token_expires_at = tok.expires_at
        db.commit()
    token = decrypt(user.access_token_enc)
    if not token:
        raise HTTPException(401, "GitHub session expired, please sign in again")
    return token


async def repo_role(db: Session, user: User, repo: DocRepo) -> str | None:
    """admin | write | read | None.

    Doc repo owner and explicit members get their role. Everyone else inherits access from
    the GitHub repos the doc repo is connected to: if you can push to the code, you can
    review its docs; if you can read the code, you can read its docs.
    """
    if repo.owner_id == user.id or user.is_admin:
        return "admin"
    best = None
    member = db.scalar(select(RepoMember).where(RepoMember.repo_id == repo.id, RepoMember.user_id == user.id))
    if member:
        best = member.role
    links = [l for l in repo.links if l.host_id == user.host_id]
    if links:
        # Inherited from the connected GitHub repos: works from the very first sign-in.
        role = inherited_role(db, user, [l.id for l in links])
        if ROLE_RANK[role] > ROLE_RANK[best]:
            best = role
        # The user's own, freshest view of their GitHub access (may be newer than the last member sync).
        await ensure_access_fresh(db, user)
        perms = access_by_repo(db, user)
        for link in links:
            role = _GH_TO_ROLE.get(perms.get(link.gh_repo_id))
            if ROLE_RANK[role] > ROLE_RANK[best]:
                best = role
    return best


async def require_repo(db: Session, user: User, owner: str, name: str, min_role: str = "read") -> tuple[DocRepo, str]:
    login, _, host = owner.partition("@")
    owner_user = db.scalar(select(User).where(User.login == login, User.host_id == (host or "github")))
    repo = db.scalar(select(DocRepo).where(DocRepo.owner_id == owner_user.id, DocRepo.name == name)) \
        if owner_user else None
    if not repo:
        raise HTTPException(404, "Repository not found")
    role = await repo_role(db, user, repo)
    if ROLE_RANK[role] < ROLE_RANK["read"]:
        raise HTTPException(404, "Repository not found")
    if ROLE_RANK[role] < ROLE_RANK[min_role]:
        raise HTTPException(403, f"You need {min_role} access to this repository")
    return repo, role


def get_pr(db: Session, repo: DocRepo, number: int) -> DocPullRequest:
    pr = db.scalar(select(DocPullRequest).where(DocPullRequest.doc_repo_id == repo.id,
                                                DocPullRequest.number == number))
    if not pr:
        raise HTTPException(404, "Pull request not found")
    return pr
