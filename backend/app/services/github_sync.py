"""On-demand "Check GitHub for changes", for setups where GitHub can't send webhooks.

When DocTheCode runs somewhere GitHub can't reach (a private service, behind a firewall), it can't
receive webhooks. Instead, a user clicks *Check GitHub for changes*: DocTheCode asks GitHub for the
connected repositories' recently updated pull requests (a conditional request, free when nothing
changed) and turns new PRs, new commits, title changes, merges, closes and reopens into the same
events webhooks deliver. It only uses outgoing connections. Nothing runs on a timer.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import select

from ..db import SessionLocal
from ..github.client import GitHubError
from ..github.registry import github_client
from ..models import DocPullRequest, RepoLink
from ..pr_events import Known, detect_events
from . import telemetry
from .sync import handle_pull_request_event

log = logging.getLogger(__name__)


async def check_link(link_id: str) -> dict[str, int]:
    """Check one connected repo now. Returns how many of each change were found and handled."""
    with SessionLocal() as db:
        link = db.get(RepoLink, link_id)
        if not link or not link.enabled:
            return {}
        host_id, repo_id, full_name, inst = link.host_id, link.gh_repo_id, link.gh_full_name, link.installation_id
        owner, name = link.owner, link.repo
        since = link.poll_cursor or link.created_at.timestamp()
        etag = link.poll_etag
        known = {p.gh_pr_number: Known(p.gh_pr_state, p.gh_head_sha, p.title)
                 for p in db.scalars(select(DocPullRequest).where(DocPullRequest.link_id == link.id))}

    gh = github_client(host_id)
    try:
        token = await gh.installation_token(inst)
        pulls, new_etag = await gh.recent_pulls(token, owner, name, etag)
    except GitHubError as e:
        _save(link_id, error=str(e)[:300])
        raise
    if pulls is None:  # 304: nothing changed
        _save(link_id)
        return {}

    events, cursor = detect_events(pulls, known, since)
    counts: dict[str, int] = {}
    for action, pr in events:
        counts[action] = counts.get(action, 0) + 1
        payload = {"action": action, "pull_request": pr, "installation": {"id": inst},
                   "repository": {"id": repo_id, "full_name": full_name}}
        result = await handle_pull_request_event(host_id, payload)
        log.info("checked %s#%s %s: %s", full_name, pr["number"], action, result)
    _save(link_id, cursor=cursor, etag=new_etag)
    return counts


def _save(link_id: str, cursor: float | None = None, etag: str | None = None, error: str | None = None) -> None:
    with SessionLocal() as db:
        link = db.get(RepoLink, link_id)
        if not link:
            return
        link.polled_at, link.poll_error = time.time(), error
        if cursor is not None:
            link.poll_cursor = cursor
        if etag is not None:
            link.poll_etag = etag
        db.commit()


async def check_links(link_ids: list[str]) -> dict:
    """Check several connected repos; one failing repo doesn't stop the others."""
    totals: dict[str, int] = {}
    errors: list[str] = []
    for link_id in link_ids:
        try:
            for action, n in (await check_link(link_id)).items():
                totals[action] = totals.get(action, 0) + n
        except (GitHubError, KeyError) as e:
            with SessionLocal() as db:
                link = db.get(RepoLink, link_id)
                errors.append(f"{link.gh_full_name if link else link_id}: {e}")
    telemetry.track("github_checked", repos=len(link_ids), errors=len(errors),
                    **{f"found_{a}": n for a, n in totals.items()})
    return {"repos": len(link_ids), "changes": totals, "errors": errors}
