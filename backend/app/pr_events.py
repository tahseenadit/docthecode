"""Turn GitHub's list of pull requests into the events DocTheCode reacts to (no I/O, easy to test).

DocTheCode normally hears about pull requests through webhooks, but those need GitHub to reach
DocTheCode over the internet. When DocTheCode runs privately (behind a company firewall, on a service
that isn't public, ...), "Check GitHub for changes" asks GitHub for recently updated pull
requests instead and feeds the same handlers with these synthesized events. Every handler is
idempotent, so it doesn't matter if a webhook and a check report the same change.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Known:
    """What DocTheCode already knows about a code PR (from its doc PR)."""
    gh_pr_state: str | None   # open | closed | merged
    head_sha: str | None
    title: str


def updated_ts(pr: dict) -> float:
    return datetime.fromisoformat(pr["updated_at"].replace("Z", "+00:00")).timestamp()


def detect_events(pulls: list[dict], known: dict[int, Known], since: float) -> tuple[list[tuple[str, dict]], float]:
    """Events for PRs updated at or after `since`, oldest first. Returns (events, new cursor)."""
    events: list[tuple[str, dict]] = []
    cursor = since
    for pr in sorted(pulls, key=updated_ts):
        ts = updated_ts(pr)
        if ts < since:
            continue
        cursor = max(cursor, ts)
        pr = {**pr, "merged": pr.get("merged_at") is not None}
        k = known.get(pr["number"])
        is_open = pr.get("state") == "open"
        if k is None:
            if is_open and not pr.get("draft"):
                events.append(("opened", pr))
            continue
        if is_open:
            if k.gh_pr_state != "open":
                events.append(("reopened", pr))
            elif k.head_sha != pr["head"]["sha"]:
                events.append(("synchronize", pr))
            if k.title != f"Docs: {pr['title']}":
                events.append(("edited", pr))
        elif k.gh_pr_state == "open":
            events.append(("closed", pr))
    return events, cursor
