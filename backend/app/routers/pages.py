"""Page-tree view of a doc repo: titles, sections, child pages, move and rename."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user, require_repo
from ..gitstore import GitError, RefMoved
from ..models import DocPullRequest, DocRepo, User
from ..pages import build_tree, is_page, plan_move, plan_new_page, section_files, set_title
from ..services import telemetry
from ..services.sync import git_store, refresh_check, schedule
from .repos import author_of

router = APIRouter(prefix="/api/repos/{owner}/{name}/pages", tags=["pages"])


def _check_branch(db: Session, repo: DocRepo, branch: str) -> DocPullRequest | None:
    pr = db.scalar(select(DocPullRequest).where(DocPullRequest.doc_repo_id == repo.id,
                                                DocPullRequest.head_branch == branch))
    if pr and pr.state != "open":
        raise HTTPException(409, "This pull request is no longer open")
    return pr


def _commit(db: Session, repo: DocRepo, user: User, branch: str, changes: dict, message: str,
            expected_sha: str | None = None) -> str:
    pr = _check_branch(db, repo, branch)
    try:
        sha = git_store().commit(repo.id, branch, changes, message, author_of(user), expected_sha=expected_sha)
    except RefMoved:
        raise HTTPException(409, "The docs changed since you loaded them. Reload and try again.")
    except (GitError, ValueError) as e:
        raise HTTPException(400, str(e))
    if pr:
        schedule(refresh_check(pr.id))  # new doc commit: approvals on the code PR need refreshing
    return sha


@router.get("")
async def tree(owner: str, name: str, ref: str | None = None, db: Session = Depends(get_db),
               user: User = Depends(current_user)):
    repo, role = await require_repo(db, user, owner, name)
    store = git_store()
    ref = ref or repo.default_branch
    try:
        files = store.list_files(repo.id, ref)
        contents = store.read_many(repo.id, ref, [f for f in files if is_page(f)])
        commit = store.resolve(repo.id, ref)
    except GitError as e:
        raise HTTPException(404, str(e))
    return {"ref": ref, "commit": commit, "role": role, **build_tree(files, contents)}


class NewPage(BaseModel):
    branch: str
    parent: str = ""            # "" for top level, a section dir, or any page (it becomes a section)
    title: str = Field(min_length=1, max_length=200)
    body: str = ""


@router.post("", status_code=201)
async def create_page(owner: str, name: str, body: NewPage, db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    repo, _ = await require_repo(db, user, owner, name, "write")
    store = git_store()
    try:
        path, changes = plan_new_page(store.list_files(repo.id, body.branch), body.parent, body.title, body.body)
    except (GitError, ValueError) as e:
        raise HTTPException(400, str(e))
    sha = _commit(db, repo, user, body.branch, changes, f"Add page “{body.title.strip()}”")
    telemetry.track("page_created", user=user, is_child=bool(body.parent))
    return {"path": path, "commit": sha}


class MovePage(BaseModel):
    branch: str
    path: str
    new_parent: str = ""


@router.post("/move")
async def move_page(owner: str, name: str, body: MovePage, db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    repo, _ = await require_repo(db, user, owner, name, "write")
    store = git_store()
    try:
        dest, changes = plan_move(store.list_files(repo.id, body.branch), body.path, body.new_parent)
    except (GitError, ValueError) as e:
        raise HTTPException(400, str(e))
    if not changes:
        return {"path": dest}
    _commit(db, repo, user, body.branch, changes, f"Move {body.path} to {body.new_parent or 'top level'}")
    telemetry.track("page_moved", user=user)
    return {"path": dest}


class RenamePage(BaseModel):
    branch: str
    path: str
    title: str = Field(min_length=1, max_length=200)


@router.post("/rename")
async def rename_page(owner: str, name: str, body: RenamePage, db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    """Changes the title only. The file keeps its path, so links to it keep working."""
    repo, _ = await require_repo(db, user, owner, name, "write")
    store = git_store()
    content = store.read_file(repo.id, body.branch, body.path)
    if content is None or not is_page(body.path):
        raise HTTPException(404, "Page not found")
    _commit(db, repo, user, body.branch, {body.path: set_title(content, body.title)},
            f"Rename page to “{body.title.strip()}”")
    return {"path": body.path}


class DeletePage(BaseModel):
    branch: str
    path: str


@router.post("/delete")
async def delete_page(owner: str, name: str, body: DeletePage, db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    """Deletes a page, or a section with all of its child pages and attachments."""
    repo, _ = await require_repo(db, user, owner, name, "write")
    doomed = section_files(git_store().list_files(repo.id, body.branch), body.path)
    if not doomed:
        raise HTTPException(404, "Page not found")
    _commit(db, repo, user, body.branch, {f: None for f in doomed}, f"Delete {body.path}")
    return {"deleted": doomed}
