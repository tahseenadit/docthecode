"""Keeps doc PRs in lockstep with GitHub code PRs.

Flow for one code PR:

  opened ──► doc PR + branch created ──► agent drafts docs ──► reviewers approve in DocTheCode
     │                                       ▲                         │
  synchronize (new commits) ─────────────────┘                         ▼
                                                    check run on the code PR turns green
  closed+merged ──► doc PR merged into the doc repo's default branch
  closed ──► doc PR closed

Background work runs as asyncio tasks inside the API process (one lock per doc PR).
For multiple replicas, swap `schedule()` for a real queue (see README).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent.copilot_agent import run_with_copilot
from ..agent.doc_agent import CodeContext, DocAgent, DocWorkspace
from ..agent.providers import messages_api
from ..config import get_settings
from ..db import SessionLocal
from ..github.client import GitHubClient, GitHubError
from ..github.registry import github_client
from ..gitstore import Author, GitError, GitStore, MergeConflict
from ..models import DocPullRequest, DocRepo, RepoLink, Review
from .gate import GateInput, ReviewInfo, evaluate
from . import telemetry
from .settings import agent_config

log = logging.getLogger(__name__)

AGENT_AUTHOR = Author("DocTheCode Agent", "agent@docthecode.local")
_locks: dict[str, asyncio.Lock] = {}
_tasks: set[asyncio.Task] = set()


def git_store() -> GitStore:
    from .repo_storage import shared_git_store

    return shared_git_store()


def schedule(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _lock(pr_id: str) -> asyncio.Lock:
    return _locks.setdefault(pr_id, asyncio.Lock())


def pr_url(pr: DocPullRequest) -> str:
    repo = pr.doc_repo
    from ..urls import public_url

    return f"{public_url()}/{repo.owner.handle}/{repo.name}/pulls/{pr.number}"


def doc_branch_for(link: RepoLink, number: int) -> str:
    return f"docthecode/{link.repo}/pr-{number}"


# --------------------------------------------------------------------- webhooks
_repo_locks: dict[tuple[str, int], asyncio.Lock] = {}
LAST_WEBHOOK: dict[str, float] = {}  # host id -> when GitHub last reached DocTheCode directly


async def handle_pull_request_event(host_id: str, payload: dict) -> str:
    """Handle a PR change, from a webhook or a "Check GitHub for changes". One at a time per repo."""
    key = (host_id, payload["repository"]["id"])
    async with _repo_locks.setdefault(key, asyncio.Lock()):
        return await _handle_pull_request_event(host_id, payload)


async def _handle_pull_request_event(host_id: str, payload: dict) -> str:
    action = payload["action"]
    gh_repo, gh_pr = payload["repository"], payload["pull_request"]
    with SessionLocal() as db:
        link = db.scalar(select(RepoLink).where(RepoLink.host_id == host_id, RepoLink.gh_repo_id == gh_repo["id"]))
        if not link or not link.enabled:
            return "ignored: repo not linked"
        link.gh_full_name = gh_repo["full_name"]
        if link.installation_id != payload.get("installation", {}).get("id", link.installation_id):
            link.installation_id = payload["installation"]["id"]
        db.commit()  # git storage writes use their own DB connection; don't hold a write lock across them
        pr = db.scalar(select(DocPullRequest).where(DocPullRequest.link_id == link.id,
                                                    DocPullRequest.gh_pr_number == gh_pr["number"]))

        if action in ("opened", "reopened", "ready_for_review") and (pr is None or pr.state == "closed"):
            if gh_pr.get("draft") and action == "opened":
                return "ignored: draft PR (docs are drafted when it is marked ready)"
            reopened = pr is not None
            pr = open_doc_pr(db, link, gh_pr) if pr is None else _reopen(db, pr, gh_pr)
            db.commit()
            telemetry.track("doc_pr_opened", reopened=reopened, code_files=gh_pr.get("changed_files"),
                            code_additions=gh_pr.get("additions"))
            schedule(run_agent(pr.id))
            schedule(_post_link_comment(pr.id))
            return f"doc PR #{pr.number} opened"

        if pr is None:
            return "ignored: no doc PR for this code PR"

        if action == "synchronize":
            pr.gh_head_sha = gh_pr["head"]["sha"]
            db.commit()
            telemetry.track("code_pr_updated", doc_pr_state=pr.state)
            schedule(run_agent(pr.id))
            return "agent re-run scheduled"

        if action == "edited":
            pr.title = f"Docs: {gh_pr['title']}"
            pr.gh_base_ref = gh_pr["base"]["ref"]
            db.commit()
            return "doc PR updated"

        if action == "closed":
            pr.gh_pr_state = "merged" if gh_pr.get("merged") else "closed"
            db.commit()
            if gh_pr.get("merged"):
                approved = evaluate(gate_for(pr)).passing
                result = merge_doc_pr(db, pr, merged_by="DocTheCode (code PR merged)")
                telemetry.track("doc_pr_merged", via="code_pr", result=result.split(" ")[0],
                                gate_passed=approved)  # gate_passed=False means someone bypassed the check
                return result
            if pr.state == "open":
                pr.state = "closed"
                db.commit()
                telemetry.track("doc_pr_closed", via="code_pr")
            return "doc PR closed"
    return "ignored"


async def handle_check_run_event(host_id: str, payload: dict) -> str:
    """Someone clicked "Re-run" on our check in GitHub: re-evaluate (and redraft if the agent failed)."""
    if payload["action"] != "rerequested":
        return "ignored"
    check_id = payload["check_run"]["id"]
    with SessionLocal() as db:
        pr = db.scalar(select(DocPullRequest).where(DocPullRequest.gh_check_run_id == check_id))
        if not pr:
            return "ignored: unknown check run"
        if pr.agent_status == "failed":
            schedule(run_agent(pr.id, force=True))
        else:
            schedule(refresh_check(pr.id))
    return "re-evaluating"


# --------------------------------------------------------------------- doc PR lifecycle
def open_doc_pr(db: Session, link: RepoLink, gh_pr: dict) -> DocPullRequest:
    repo = db.get(DocRepo, link.doc_repo_id)
    store = git_store()
    branch = doc_branch_for(link, gh_pr["number"])
    if store.branch_sha(repo.id, branch):
        store.reset_branch(repo.id, branch, repo.default_branch)
    else:
        store.create_branch(repo.id, branch, repo.default_branch)
    number = repo.next_pr_number
    repo.next_pr_number += 1
    pr = DocPullRequest(
        doc_repo_id=repo.id, number=number, title=f"Docs: {gh_pr['title']}",
        body=f"Documentation for {link.gh_full_name}#{gh_pr['number']}.",
        head_branch=branch, base_branch=repo.default_branch, link_id=link.id,
        gh_pr_number=gh_pr["number"], gh_pr_url=gh_pr["html_url"], gh_pr_author=gh_pr["user"]["login"],
        gh_pr_state="open", gh_head_sha=gh_pr["head"]["sha"], gh_base_ref=gh_pr["base"]["ref"],
        agent_status="queued",
    )
    db.add(pr)
    db.flush()
    return pr


def _reopen(db: Session, pr: DocPullRequest, gh_pr: dict) -> DocPullRequest:
    store = git_store()
    if not store.branch_sha(pr.doc_repo_id, pr.head_branch):
        store.create_branch(pr.doc_repo_id, pr.head_branch, pr.base_branch)
    pr.state, pr.gh_pr_state, pr.gh_head_sha, pr.agent_status = "open", "open", gh_pr["head"]["sha"], "queued"
    return pr


def merge_doc_pr(db: Session, pr: DocPullRequest, merged_by: str) -> str:
    if pr.state != "open":
        return f"doc PR already {pr.state}"
    store = git_store()
    try:
        if not store.changed_files(pr.doc_repo_id, pr.base_branch, pr.head_branch):
            pr.state = "closed"
            pr.agent_log = [*pr.agent_log, "Closed: no documentation changes to merge."]
            db.commit()
            return "doc PR closed (no changes)"
        sha = store.merge(pr.doc_repo_id, pr.base_branch, pr.head_branch,
                          f"Merge doc PR #{pr.number}: {pr.title}\n\nMerged by {merged_by}.",
                          Author("DocTheCode", "docthecode@localhost"))
    except MergeConflict as e:
        pr.agent_log = [*pr.agent_log, f"⚠️ Could not auto-merge: conflicts in {', '.join(e.paths)}. "
                                       "Resolve on the doc branch and merge in DocTheCode."]
        db.commit()
        return "merge conflict"
    pr.state, pr.merged_sha = "merged", sha
    db.commit()
    return f"doc PR merged at {sha[:12]}"


# --------------------------------------------------------------------- agent
def _feedback(pr: DocPullRequest) -> list[Review]:
    return [r for r in pr.reviews if r.state in ("changes_requested", "commented")
            and r.body.strip() and not r.addressed_by_agent]


async def run_agent(pr_id: str, force: bool = False) -> None:
    async with _lock(pr_id):
        try:
            await _run_agent_locked(pr_id, force)
        except Exception as e:  # never let a background task die silently
            log.exception("agent run failed for %s", pr_id)
            telemetry.track("agent_run", status="crashed", error=type(e).__name__)
            with SessionLocal() as db:
                pr = db.get(DocPullRequest, pr_id)
                if pr:
                    pr.agent_status = "failed"
                    pr.agent_log = [*pr.agent_log, f"❌ Agent failed: {e}"]
                    db.commit()
        await refresh_check(pr_id)


async def _run_agent_locked(pr_id: str, force: bool) -> None:
    settings = get_settings()
    store = git_store()
    with SessionLocal() as db:
        pr = db.get(DocPullRequest, pr_id)
        if not pr or pr.state != "open" or not pr.link:
            return
        feedback = _feedback(pr)
        if pr.agent_processed_sha == pr.gh_head_sha and not feedback and not force:
            return  # nothing new
        link, repo = pr.link, pr.doc_repo
        pr.agent_status = "running"
        db.commit()
        await refresh_check(pr_id)

        gh = github_client(link.host_id)
        token = await gh.installation_token(link.installation_id)
        gh_pr = await gh.get_pull(token, link.owner, link.repo, pr.gh_pr_number)
        head_sha = gh_pr["head"]["sha"]
        files = await gh.pull_files(token, link.owner, link.repo, pr.gh_pr_number)

        incremental = bool(pr.agent_processed_sha and pr.agent_processed_sha != head_sha)
        try:
            diff = await gh.compare_diff(token, link.owner, link.repo,
                                         pr.agent_processed_sha if incremental else gh_pr["base"]["sha"], head_sha)
        except GitHubError:  # e.g. force-push removed the old sha: fall back to the whole PR
            incremental = False
            diff = await gh.compare_diff(token, link.owner, link.repo, gh_pr["base"]["sha"], head_sha)

        ctx = CodeContext(link.gh_full_name, pr.gh_pr_number, gh_pr["title"], gh_pr.get("body") or "",
                          gh_pr["user"]["login"], gh_pr["base"]["ref"], head_sha, files, diff, incremental)
        branch_sha = store.branch_sha(repo.id, pr.head_branch)
        ws = DocWorkspace(lambda: store.list_files(repo.id, branch_sha),
                          lambda p: store.read_file(repo.id, branch_sha, p))
        code_tree: list[str] = []

        async def read_code(path: str):
            return await gh.file_content(token, link.owner, link.repo, path, head_sha)

        async def list_code():
            if not code_tree:
                code_tree.extend(await gh.tree(token, link.owner, link.repo, head_sha))
            return code_tree

        cfg = agent_config()
        if cfg.problem():
            raise RuntimeError(cfg.problem())
        model = cfg.effective_model
        existing = store.diff(repo.id, pr.base_branch, branch_sha)
        notes = [f"{r.user.login}: {r.body}" for r in feedback]
        started = time.monotonic()
        if cfg.provider == "copilot":
            # Billed to the code repo's organization through DocTheCode's GitHub App installation.
            result = await run_with_copilot(ctx, ws, read_code, list_code, github_token=token, model=model,
                                            instructions=link.agent_instructions, feedback=notes,
                                            existing_doc_diff=existing)
        else:
            agent = DocAgent(messages_api(cfg), model, settings.agent_max_turns)
            result = await agent.run(ctx, ws, read_code, list_code, instructions=link.agent_instructions,
                                     feedback=notes, existing_doc_diff=existing)
        telemetry.track("agent_run", status="finished" if result.finished else "turn_limit", model=model,
                        provider=cfg.provider,
                        seconds=round(time.monotonic() - started, 1), turns=result.turns,
                        files_changed=len(result.changes), files_deleted=sum(1 for c in result.changes.values() if c is None),
                        incremental=incremental, feedback=len(feedback), code_files=len(files),
                        diff_kb=round(len(diff) / 1024), has_instructions=bool(link.agent_instructions.strip()))

        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        log_entry = [f"── Run at {stamp} for {head_sha[:12]}{' (incremental)' if incremental else ''}",
                     *result.transcript]
        if result.changes:
            msg = f"Update docs for {link.gh_full_name}#{pr.gh_pr_number} ({head_sha[:7]})\n\n{result.summary}"
            new_sha = store.commit(repo.id, pr.head_branch, result.changes, msg, AGENT_AUTHOR,
                                   expected_sha=branch_sha)
            log_entry.append(f"✅ Committed {len(result.changes)} file(s) as {new_sha[:12]}")
        else:
            log_entry.append("✅ No documentation changes made in this run.")

        pr.agent_summary = result.summary
        pr.agent_log = [*pr.agent_log, *log_entry][-400:]
        pr.agent_processed_sha = head_sha
        has_changes = bool(store.changed_files(repo.id, pr.base_branch, pr.head_branch))
        pr.agent_status = ("done" if has_changes else "no_changes") if result.finished else "failed"
        for r in feedback:
            r.addressed_by_agent = True
        db.commit()
    schedule(_update_link_comment(pr_id))


# --------------------------------------------------------------------- GitHub check run
def gate_for(pr: DocPullRequest) -> GateInput:
    store = git_store()
    head = store.branch_sha(pr.doc_repo_id, pr.head_branch) or ""
    has_changes = bool(head) and bool(store.changed_files(pr.doc_repo_id, pr.base_branch, head))
    reviews = tuple(ReviewInfo(r.user.handle, r.state, r.doc_head_sha, r.created_at.timestamp())
                    for r in pr.reviews)
    link = pr.link
    return GateInput(pr.state, pr.agent_status, head, reviews,
                     required_approvals=link.required_approvals if link else 1,
                     pass_when_no_changes=link.pass_when_no_changes if link else False,
                     has_doc_changes=has_changes)


async def refresh_check(pr_id: str) -> None:
    """Create or update the check run on the code PR's current head commit."""
    settings = get_settings()
    with SessionLocal() as db:
        pr = db.get(DocPullRequest, pr_id)
        if not pr or not pr.link or not pr.gh_head_sha:
            return
        result = evaluate(gate_for(pr))
        link = pr.link
        gh: GitHubClient = github_client(link.host_id)
        try:
            token = await gh.installation_token(link.installation_id)
            body = {
                "name": settings.check_name, "details_url": pr_url(pr), "external_id": pr.id,
                "status": result.status,
                "output": {"title": result.title,
                           "summary": f"{result.summary}\n\n[Open doc PR #{pr.number} in DocTheCode]({pr_url(pr)})"
                                      + (f"\n\n### Agent summary\n{pr.agent_summary}" if pr.agent_summary else "")},
            }
            if result.status == "completed":
                body["conclusion"] = result.conclusion
            if pr.gh_check_run_id and pr.gh_check_run_sha == pr.gh_head_sha:
                await gh.update_check_run(token, link.owner, link.repo, pr.gh_check_run_id, **body)
            else:  # check runs belong to a commit; a new push needs a new check run
                run = await gh.create_check_run(token, link.owner, link.repo, head_sha=pr.gh_head_sha, **body)
                pr.gh_check_run_id, pr.gh_check_run_sha = run["id"], pr.gh_head_sha
                db.commit()
        except GitHubError:
            log.exception("could not update check run for doc PR %s", pr_id)


async def _post_link_comment(pr_id: str) -> None:
    with SessionLocal() as db:
        pr = db.get(DocPullRequest, pr_id)
        if not pr or pr.gh_comment_id or not pr.link:
            return
        link, gh = pr.link, github_client(pr.link.host_id)
        try:
            token = await gh.installation_token(link.installation_id)
            c = await gh.comment(token, link.owner, link.repo, pr.gh_pr_number, _comment_body(pr))
            pr.gh_comment_id = c["id"]
            db.commit()
        except GitHubError:
            log.exception("could not comment on code PR")


async def _update_link_comment(pr_id: str) -> None:
    with SessionLocal() as db:
        pr = db.get(DocPullRequest, pr_id)
        if not pr or not pr.gh_comment_id or not pr.link:
            return
        link, gh = pr.link, github_client(pr.link.host_id)
        try:
            token = await gh.installation_token(link.installation_id)
            await gh.update_comment(token, link.owner, link.repo, pr.gh_comment_id, _comment_body(pr))
        except GitHubError:
            log.exception("could not update code PR comment")


def _comment_body(pr: DocPullRequest) -> str:
    repo = pr.doc_repo
    lines = [f"📚 **DocTheCode** opened [{repo.owner.handle}/{repo.name}#{pr.number}]({pr_url(pr)}) "
             "to keep the documentation in sync with this PR.",
             "",
             "This PR can be merged once the documentation PR is approved in DocTheCode "
             f"(check: `{get_settings().check_name}`)."]
    if pr.agent_summary:
        lines += ["", "<details><summary>Agent summary</summary>", "", pr.agent_summary, "", "</details>"]
    return "\n".join(lines)


__all__ = ["handle_pull_request_event", "handle_check_run_event", "run_agent", "refresh_check",
           "merge_doc_pr", "open_doc_pr", "git_store", "schedule", "GitError"]
