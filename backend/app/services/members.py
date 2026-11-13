"""Doc repo membership inherited from connected GitHub repos.

When a doc repo is connected to a GitHub repo, everyone with access to that GitHub repo
(direct collaborators, team members, org members through the base permission, outside
collaborators) becomes a member of the doc repo with the matching role. Nobody needs an
invite, and they have access from their very first sign-in.

The list stays in sync with GitHub: right after connecting, whenever GitHub sends a
`member` webhook, hourly in the background, and on demand from the doc repo's settings.
It is stored only in the organization's own DocTheCode database. The vendor's usage data
gets nothing but counts.
"""
from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..github.client import GitHubError
from ..github.registry import github_client
from ..models import InheritedMember, RepoLink, User
from . import telemetry
from ..membership import collaborators_from_api, plan_member_sync, role_for  # noqa: F401

log = logging.getLogger(__name__)
STALE_AFTER = 3600            # refresh a link's members at most hourly in the background
_locks: dict[str, asyncio.Lock] = {}


async def sync_link_members(db: Session, link: RepoLink, reason: str) -> dict:
    """Mirror the GitHub repo's people and permissions onto the link. Returns a small summary."""
    lock = _locks.setdefault(link.id, asyncio.Lock())
    async with lock:
        gh = github_client(link.host_id)
        try:
            token = await gh.installation_token(link.installation_id)
            fetched = collaborators_from_api(await gh.repo_collaborators(token, link.owner, link.repo))
        except GitHubError as e:
            link.members_sync_error = str(e)[:500]
            db.commit()
            telemetry.track("members_sync", reason=reason, ok=False, status=e.status)
            raise

        rows = {m.github_id: m for m in link.inherited}
        added, changed, removed = plan_member_sync({g: (m.login, m.permission) for g, m in rows.items()}, fetched)
        for c in added:
            db.add(InheritedMember(link_id=link.id, host_id=link.host_id, github_id=c.github_id, login=c.login,
                                   avatar_url=c.avatar_url, permission=c.permission))
        for c in changed:
            m = rows[c.github_id]
            m.login, m.permission, m.avatar_url = c.login, c.permission, c.avatar_url
        for gid in removed:
            db.delete(rows[gid])
        link.members_synced_at = time.time()
        link.members_sync_error = None
        db.commit()

        perms = [c.permission for c in fetched]
        telemetry.track("members_sync", reason=reason, ok=True, members=telemetry.bucket(len(fetched)),
                        added=len(added), changed=len(changed), removed=len(removed),
                        admins=perms.count("admin"), writers=perms.count("write") + perms.count("maintain"),
                        readers=perms.count("read") + perms.count("triage"))
        return {"members": len(fetched), "added": len(added), "changed": len(changed), "removed": len(removed)}


def inherited_role(db: Session, user: User, link_ids: list[str]) -> str | None:
    if not link_ids:
        return None
    perms = db.scalars(select(InheritedMember.permission).where(
        InheritedMember.link_id.in_(link_ids), InheritedMember.host_id == user.host_id,
        InheritedMember.github_id == user.github_id))
    rank = {None: 0, "read": 1, "write": 2, "admin": 3}
    best = None
    for p in perms:
        r = role_for(p)
        if rank[r] > rank[best]:
            best = r
    return best


async def refresh_stale_links() -> None:
    with SessionLocal() as db:
        cutoff = time.time() - STALE_AFTER
        links = db.scalars(select(RepoLink).where(RepoLink.enabled.is_(True)).where(
            (RepoLink.members_synced_at.is_(None)) | (RepoLink.members_synced_at < cutoff))).all()
        for link in links:
            try:
                await sync_link_members(db, link, reason="scheduled")
            except (GitHubError, KeyError) as e:
                log.warning("member sync failed for %s: %s", link.gh_full_name, e)
                db.rollback()


async def run_forever() -> None:
    await asyncio.sleep(10)
    while True:
        try:
            await refresh_stale_links()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("background member sync crashed")
        await asyncio.sleep(300)
