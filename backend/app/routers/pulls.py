from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import ROLE_RANK, current_user, get_pr, require_repo
from ..gitstore import GitError, validate_branch
from ..models import DocPullRequest, Review, User
from ..services.gate import evaluate
from ..services.sync import gate_for, git_store, merge_doc_pr, refresh_check, run_agent, schedule
from ..services import telemetry

router = APIRouter(prefix="/api/repos/{owner}/{name}/pulls", tags=["pulls"])


def pr_summary(pr: DocPullRequest) -> dict:
    return {
        "number": pr.number, "title": pr.title, "state": pr.state, "head_branch": pr.head_branch,
        "base_branch": pr.base_branch, "author": pr.author.handle if pr.author else pr.gh_pr_author,
        "created_at": pr.created_at.isoformat(), "updated_at": pr.updated_at.isoformat(),
        "agent_status": pr.agent_status,
        "code_pr": pr.link and {"full_name": pr.link.gh_full_name, "number": pr.gh_pr_number,
                                "url": pr.gh_pr_url, "state": pr.gh_pr_state, "head_sha": pr.gh_head_sha},
        "gate": evaluate(gate_for(pr)).__dict__,
    }


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # SQLite returns naive datetimes


def _can_review(pr: DocPullRequest, user: User, role: str | None) -> tuple[bool, str | None]:
    if pr.state != "open":
        return False, "Pull request is not open"
    if ROLE_RANK[role] < ROLE_RANK["write"]:
        return False, "You need write access to review"
    link = pr.link
    if link and not link.allow_author_approval and user.host_id == link.host_id and user.login == pr.gh_pr_author:
        return False, "Authors of the code PR cannot approve its documentation"
    return True, None


@router.get("")
async def list_pulls(owner: str, name: str, state: str = "open", db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    repo, _ = await require_repo(db, user, owner, name)
    q = select(DocPullRequest).where(DocPullRequest.doc_repo_id == repo.id)
    if state != "all":
        q = q.where(DocPullRequest.state == state)
    return [pr_summary(pr) for pr in db.scalars(q.order_by(DocPullRequest.number.desc()).limit(200))]


class PullCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body: str = ""
    branch: str


@router.post("", status_code=201)
async def create_pull(owner: str, name: str, body: PullCreate, db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    """A hand-made doc PR (not tied to a code PR). Creates the branch if it does not exist yet."""
    repo, _ = await require_repo(db, user, owner, name, "write")
    store = git_store()
    try:
        validate_branch(body.branch)
        if body.branch == repo.default_branch:
            raise HTTPException(422, "Pick a branch other than the default branch")
        if not store.branch_sha(repo.id, body.branch):
            store.create_branch(repo.id, body.branch, repo.default_branch)
    except (GitError, ValueError) as e:
        raise HTTPException(422, str(e))
    pr = DocPullRequest(doc_repo_id=repo.id, number=repo.next_pr_number, title=body.title, body=body.body,
                        author_id=user.id, head_branch=body.branch, base_branch=repo.default_branch,
                        agent_status="idle")
    repo.next_pr_number += 1
    db.add(pr)
    db.commit()
    telemetry.track("doc_pr_created_manually", user)
    return pr_summary(pr)


@router.get("/{number}")
async def get_pull(owner: str, name: str, number: int, db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    repo, role = await require_repo(db, user, owner, name)
    pr = get_pr(db, repo, number)
    store = git_store()
    head = store.branch_sha(repo.id, pr.head_branch)
    files, diff, commits = [], "", []
    if head and pr.state != "merged":
        files = [f.__dict__ for f in store.changed_files(repo.id, pr.base_branch, head)]
        diff = store.diff(repo.id, pr.base_branch, head)
        commits = [c.__dict__ for c in store.log(repo.id, head, limit=100, since=store.merge_base(repo.id, pr.base_branch, head))]
    elif pr.merged_sha:
        commits = [c.__dict__ for c in store.log(repo.id, pr.merged_sha, limit=1)]
    can_review, review_blocker = _can_review(pr, user, role)
    return {
        **pr_summary(pr), "body": pr.body, "head_sha": head, "files": files, "diff": diff, "commits": commits,
        "agent_summary": pr.agent_summary, "agent_log": pr.agent_log, "role": role,
        "can_review": can_review, "review_blocker": review_blocker,
        "can_merge": pr.state == "open" and ROLE_RANK[role] >= ROLE_RANK["write"] and _merge_blocker(pr) is None,
        "can_reopen": pr.state == "closed" and ROLE_RANK[role] >= ROLE_RANK["write"]
                      and (not pr.link or pr.gh_pr_state == "open"),
        "merge_blocker": _merge_blocker(pr),
        "reviews": [{"id": r.id, "user": r.user.handle, "avatar_url": r.user.avatar_url, "state": r.state,
                     "body": r.body, "doc_head_sha": r.doc_head_sha, "stale": r.doc_head_sha != head,
                     "addressed_by_agent": r.addressed_by_agent, "created_at": r.created_at.isoformat()}
                    for r in pr.reviews],
    }


class ReviewCreate(BaseModel):
    state: str  # approved | changes_requested | commented
    body: str = ""
    ask_agent: bool = False  # hand the feedback to the agent to redraft


@router.post("/{number}/reviews", status_code=201)
async def review(owner: str, name: str, number: int, body: ReviewCreate, db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    repo, role = await require_repo(db, user, owner, name)
    pr = get_pr(db, repo, number)
    if body.state not in ("approved", "changes_requested", "commented"):
        raise HTTPException(422, "Invalid review state")
    if body.state != "commented":
        ok, why = _can_review(pr, user, role)
        if not ok:
            raise HTTPException(403, why)
    if body.state == "changes_requested" and not body.body.strip():
        raise HTTPException(422, "Describe what should change")
    head = git_store().branch_sha(repo.id, pr.head_branch)
    if not head:
        raise HTTPException(409, "The doc branch no longer exists")
    db.add(Review(pr_id=pr.id, user_id=user.id, state=body.state, body=body.body, doc_head_sha=head))
    db.commit()
    telemetry.track("review_submitted", user, state=body.state, ask_agent=body.ask_agent, linked=pr.link is not None,
                    is_author=bool(pr.gh_pr_author) and pr.gh_pr_author == user.login,
                    minutes_since_opened=round((datetime.now(timezone.utc) - _aware(pr.created_at)).total_seconds() / 60))
    if body.ask_agent and pr.link and body.body.strip():
        pr.agent_status = "queued"
        db.commit()
        schedule(run_agent(pr.id))
    else:
        schedule(refresh_check(pr.id))
    return {"ok": True}


class AgentRun(BaseModel):
    instructions: str = ""


@router.post("/{number}/agent")
async def rerun_agent(owner: str, name: str, number: int, body: AgentRun, db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    repo, _ = await require_repo(db, user, owner, name, "write")
    pr = get_pr(db, repo, number)
    if not pr.link or pr.state != "open":
        raise HTTPException(409, "The agent only runs on open doc PRs that mirror a code PR")
    if pr.agent_status in ("queued", "running"):
        raise HTTPException(409, "The agent is already running")
    if body.instructions.strip():
        head = git_store().branch_sha(repo.id, pr.head_branch)
        db.add(Review(pr_id=pr.id, user_id=user.id, state="commented", body=body.instructions, doc_head_sha=head))
    pr.agent_status = "queued"
    db.commit()
    telemetry.track("agent_rerun_requested", user, with_instructions=bool(body.instructions.strip()))
    schedule(run_agent(pr.id, force=True))
    return {"ok": True}


def _merge_blocker(pr: DocPullRequest) -> str | None:
    if pr.state != "open":
        return f"Pull request is {pr.state}"
    if pr.link and pr.gh_pr_state != "merged":
        return "Merges automatically when the code PR is merged on GitHub"
    if not pr.link and not any(r.state == "approved" for r in pr.reviews):
        return "Needs an approval"
    return None


@router.post("/{number}/merge")
async def merge(owner: str, name: str, number: int, db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    repo, _ = await require_repo(db, user, owner, name, "write")
    pr = get_pr(db, repo, number)
    blocker = _merge_blocker(pr)
    if blocker:
        raise HTTPException(409, blocker)
    result = merge_doc_pr(db, pr, merged_by=user.handle)
    telemetry.track("doc_pr_merged", user, via="manual", result=result.split(" ")[0])
    if result == "merge conflict":
        raise HTTPException(409, "Merge conflict: resolve on the doc branch first")
    return {"result": result}


@router.post("/{number}/close")
async def close(owner: str, name: str, number: int, db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    repo, _ = await require_repo(db, user, owner, name, "write")
    pr = get_pr(db, repo, number)
    if pr.state != "open":
        raise HTTPException(409, "Already closed")
    pr.state = "closed"
    db.commit()
    telemetry.track("doc_pr_closed", user, via="manual", linked=pr.link is not None)
    schedule(refresh_check(pr.id))  # a closed doc PR fails the check on the code PR
    return {"ok": True}


@router.post("/{number}/reopen")
async def reopen(owner: str, name: str, number: int, db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    repo, _ = await require_repo(db, user, owner, name, "write")
    pr = get_pr(db, repo, number)
    if pr.state != "closed":
        raise HTTPException(409, f"This pull request is {pr.state}, not closed")
    if pr.link and pr.gh_pr_state != "open":
        # A doc PR follows its code PR: when the code PR is closed or merged, the doc PR stays closed.
        raise HTTPException(409, f"The code pull request is {pr.gh_pr_state or 'not open'} on GitHub. A doc PR can only be "
                                 "reopened while its code pull request is open.")
    store = git_store()
    if not store.branch_sha(repo.id, pr.head_branch):  # e.g. deleted by hand: recreate it from the docs
        store.create_branch(repo.id, pr.head_branch, pr.base_branch)
    pr.state = "open"
    db.commit()
    telemetry.track("doc_pr_reopened", user, linked=pr.link is not None, code_pr_state=pr.gh_pr_state)
    schedule(refresh_check(pr.id))  # the check on the code PR goes back to pending / follows reviews again
    return {"ok": True}
