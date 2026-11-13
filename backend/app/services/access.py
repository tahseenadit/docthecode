"""What each user can see, derived from their GitHub access.

On sign-in (and at most every few minutes after) DocTheCode asks GitHub which repositories
the user can access through the DocTheCode App installations and stores that list. Any doc
repo connected to one of those repositories then shows up for the user automatically,
with a role that follows their GitHub permission.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from ..github.client import GitHubError
from ..github.registry import github_client
from ..membership import permission_from
from ..models import DocRepo, InheritedMember, RepoLink, RepoMember, User, UserRepoAccess

log = logging.getLogger(__name__)
ACCESS_MAX_AGE = 600  # seconds

async def sync_user_access(db: Session, user: User) -> int:
    """Refresh the user's list of accessible GitHub repos. Returns how many were found."""
    from ..deps import user_token  # local import: deps imports this module

    gh = github_client(user.host_id)
    token = await user_token(db, user)
    rows: dict[int, UserRepoAccess] = {}
    for inst in await gh.user_installations(token):
        for r in await gh.user_installation_repos(token, inst["id"]):
            rows[r["id"]] = UserRepoAccess(user_id=user.id, host_id=user.host_id, gh_repo_id=r["id"],
                                           full_name=r["full_name"], permission=permission_from(r.get("permissions")),
                                           installation_id=inst["id"])
    db.execute(delete(UserRepoAccess).where(UserRepoAccess.user_id == user.id))
    db.add_all(rows.values())
    user.access_synced_at = time.time()
    db.commit()
    return len(rows)


async def ensure_access_fresh(db: Session, user: User, max_age: int = ACCESS_MAX_AGE) -> None:
    if user.access_synced_at and user.access_synced_at > time.time() - max_age:
        return
    try:
        await sync_user_access(db, user)
    except (GitHubError, KeyError) as e:
        # Keep the last known list; GitHub may be briefly unavailable or the token revoked.
        log.warning("could not refresh GitHub access for %s: %s", user.login, e)
        db.rollback()


def access_by_repo(db: Session, user: User) -> dict[int, str]:
    """gh_repo_id -> permission, for the user's own GitHub host."""
    return {a.gh_repo_id: a.permission for a in
            db.scalars(select(UserRepoAccess).where(UserRepoAccess.user_id == user.id))}


def visible_repos(db: Session, user: User) -> list[DocRepo]:
    """Doc repos the user owns, was added to, inherited from a connected GitHub repo, or can reach via GitHub."""
    gh_ids = select(UserRepoAccess.gh_repo_id).where(UserRepoAccess.user_id == user.id)
    via_github = select(RepoLink.doc_repo_id).where(RepoLink.host_id == user.host_id, RepoLink.gh_repo_id.in_(gh_ids))
    via_member = select(RepoMember.repo_id).where(RepoMember.user_id == user.id)
    via_inherited = (select(RepoLink.doc_repo_id).join(InheritedMember, InheritedMember.link_id == RepoLink.id)
                     .where(InheritedMember.host_id == user.host_id, InheritedMember.github_id == user.github_id))
    return list(db.scalars(select(DocRepo).where(or_(DocRepo.owner_id == user.id, DocRepo.id.in_(via_member),
                                                     DocRepo.id.in_(via_github), DocRepo.id.in_(via_inherited)))
                           .order_by(DocRepo.created_at.desc())))
