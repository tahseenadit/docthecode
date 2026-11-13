from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request

from ..github.registry import github_client
from ..github.signatures import verify
from ..services.sync import LAST_WEBHOOK, handle_check_run_event, handle_pull_request_event
from ..services import telemetry

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


@router.post("/github/{host_id}")
async def github_webhook(host_id: str, request: Request):
    """One webhook URL per GitHub host, e.g. /api/webhooks/github/github or /api/webhooks/github/acme-ghe."""
    try:
        host = github_client(host_id).host
    except KeyError:
        raise HTTPException(404, "Unknown host")
    body = await request.body()
    if not verify(host.webhook_secret, body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(401, "Bad signature")
    event = request.headers.get("X-GitHub-Event", "")
    LAST_WEBHOOK[host_id] = time.time()
    payload = json.loads(body)
    telemetry.track("webhook_received", event=event[:30], action=str(payload.get("action", ""))[:30])
    if event == "pull_request":
        result = await handle_pull_request_event(host_id, payload)
    elif event == "member":  # someone was added to / removed from the GitHub repo, or their permission changed
        result = await handle_member_event(host_id, payload)
    elif event == "check_run":
        result = await handle_check_run_event(host_id, payload)
    else:
        result = f"ignored event {event}"
    log.info("webhook %s/%s: %s", event, payload.get("action"), result)
    return {"result": result}


async def handle_member_event(host_id: str, payload: dict) -> str:
    from sqlalchemy import select

    from ..db import SessionLocal
    from ..models import RepoLink
    from ..services.members import sync_link_members
    from ..services.sync import schedule

    with SessionLocal() as db:
        link = db.scalar(select(RepoLink).where(RepoLink.host_id == host_id,
                                                RepoLink.gh_repo_id == payload["repository"]["id"]))
        if not link:
            return "ignored: repo not linked"
        link_id = link.id

    async def resync():
        with SessionLocal() as db:
            await sync_link_members(db, db.get(RepoLink, link_id), reason="webhook")

    schedule(resync())
    return "member sync scheduled"
