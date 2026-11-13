from __future__ import annotations

import re
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from ..config import get_settings
from ..db import get_db
from ..deps import ROLE_RANK, current_user, repo_role, require_repo, user_token
from ..github.client import GitHubError
from ..github.registry import github_client
from ..gitstore import Author, GitError, RefMoved
from ..models import DocPullRequest, DocRepo, GitHubHostConfig, RepoLink, RepoMember, User
from ..services.access import ensure_access_fresh, sync_user_access, visible_repos
from ..services.members import role_for, sync_link_members
from ..services.sync import git_store
from ..services import telemetry

router = APIRouter(prefix="/api", tags=["repos"])
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def author_of(user: User) -> Author:
    return Author(user.name or user.login, user.email or f"{user.login}@users.noreply.docthecode")


def repo_json(repo: DocRepo, role: str | None = None) -> dict:
    return {"id": repo.id, "owner": repo.owner.handle, "name": repo.name, "full_name": f"{repo.owner.handle}/{repo.name}",
            "description": repo.description, "default_branch": repo.default_branch, "role": role,
            "created_at": repo.created_at.isoformat(),
            "links": [link_json(l) for l in repo.links]}


def link_json(l: RepoLink) -> dict:
    host = object_session(l).get(GitHubHostConfig, l.host_id)
    web_url = host.web_url if host else ""
    return {"id": l.id, "host_id": l.host_id, "host_name": host.name if host else l.host_id,
            "full_name": l.gh_full_name, "url": f"{web_url}/{l.gh_full_name}", "enabled": l.enabled,
            "required_approvals": l.required_approvals, "pass_when_no_changes": l.pass_when_no_changes,
            "allow_author_approval": l.allow_author_approval, "agent_instructions": l.agent_instructions,
            "members_count": len(l.inherited), "members_synced_at": l.members_synced_at,
            "members_sync_error": l.members_sync_error, "github_checked_at": l.polled_at,
            "github_check_error": l.poll_error}


# ------------------------------------------------------------------ repos
class RepoCreate(BaseModel):
    name: str
    description: str = ""
    default_branch: str = "main"
    initialize_with: dict[str, str] | None = None


@router.get("/repos")
async def list_repos(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Every doc repo the user owns, was added to, or can see through their GitHub access."""
    await ensure_access_fresh(db, user)
    out = []
    for r in visible_repos(db, user):
        j = repo_json(r)
        j["open_prs"] = db.query(DocPullRequest).filter_by(doc_repo_id=r.id, state="open").count()
        out.append(j)
    return out


@router.post("/me/sync")
async def sync_access(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Re-read the user's GitHub access now (e.g. right after they were added to a repo)."""
    try:
        count = await sync_user_access(db, user)
    except GitHubError as e:
        raise HTTPException(502, f"GitHub: {e}")
    telemetry.track("access_refreshed", user, github_repos=count)
    return {"github_repos": count}


@router.post("/check-github")
async def check_github_all(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """'Check GitHub for changes' on the dashboard: every connected repo behind the doc spaces you can edit."""
    from ..services.github_sync import check_links

    link_ids = []
    for repo in visible_repos(db, user):
        if repo.links and ROLE_RANK[await repo_role(db, user, repo)] >= ROLE_RANK["write"]:
            link_ids += [l.id for l in repo.links if l.enabled]
    return await check_links(link_ids)


@router.post("/repos/{owner}/{name}/check-github")
async def check_github_repo(owner: str, name: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """'Check GitHub for changes' for one doc space: picks up new, updated, merged and closed pull requests."""
    from ..services.github_sync import check_links

    repo, _ = await require_repo(db, user, owner, name, "write")
    return await check_links([l.id for l in repo.links if l.enabled])


@router.get("/inbox")
async def inbox(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Open doc PRs across every doc repo the user can see, newest first."""
    from .pulls import pr_summary  # avoid an import cycle at module load

    await ensure_access_fresh(db, user)
    repos = {r.id: r for r in visible_repos(db, user)}
    if not repos:
        return []
    prs = db.scalars(select(DocPullRequest).where(DocPullRequest.doc_repo_id.in_(repos.keys()),
                                                  DocPullRequest.state == "open")
                     .order_by(DocPullRequest.updated_at.desc()).limit(100))
    out = []
    for pr in prs:
        repo = repos[pr.doc_repo_id]
        j = pr_summary(pr)
        mine = bool(pr.gh_pr_author) and pr.gh_pr_author == user.login and (pr.link and pr.link.host_id == user.host_id)
        approved_by_me = any(r.user_id == user.id and r.state == "approved" for r in pr.reviews)
        j.update({"repo": f"{repo.owner.handle}/{repo.name}", "is_mine": bool(mine),
                  "needs_review": not approved_by_me and j["gate"]["conclusion"] not in ("success", "neutral")})
        out.append(j)
    return out


@router.post("/repos", status_code=201)
def create_repo(body: RepoCreate, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not NAME_RE.match(body.name):
        raise HTTPException(422, "Name may contain letters, digits, '.', '-' and '_'")
    repo = DocRepo(owner_id=user.id, name=body.name, description=body.description,
                   default_branch=body.default_branch)
    db.add(repo)
    try:
        db.commit()  # commit before writing git data: storage uses its own DB connection
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "You already have a repository with that name")
    files = body.initialize_with or {
        "index.md": f"# {body.name}\n\n{body.description or 'Welcome to the documentation.'}\n\n"
                    "Use **New page** to add pages and sections. Pages are organized for readers, "
                    "independently of how the code is laid out.\n",
    }
    try:
        git_store().init_repo(repo.id, repo.default_branch, author_of(user), files)
    except Exception:
        db.delete(repo)
        db.commit()
        raise
    telemetry.track("repo_created", user)
    return repo_json(repo, "admin")


@router.get("/repos/{owner}/{name}")
async def get_repo(owner: str, name: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    repo, role = await require_repo(db, user, owner, name)
    j = repo_json(repo, role)
    j["branches"] = git_store().list_branches(repo.id)
    return j


class RepoUpdate(BaseModel):
    description: str | None = None


@router.patch("/repos/{owner}/{name}")
async def update_repo(owner: str, name: str, body: RepoUpdate, db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    repo, role = await require_repo(db, user, owner, name, "admin")
    if body.description is not None:
        repo.description = body.description
    db.commit()
    return repo_json(repo, role)


@router.delete("/repos/{owner}/{name}", status_code=204)
async def delete_repo(owner: str, name: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    repo, _ = await require_repo(db, user, owner, name, "admin")
    if repo.owner_id != user.id:
        raise HTTPException(403, "Only the owner can delete a repository")
    db.query(DocPullRequest).filter_by(doc_repo_id=repo.id).delete()
    db.delete(repo)
    db.commit()
    git_store().delete_repo(repo.id)


# ------------------------------------------------------------------ files
@router.get("/repos/{owner}/{name}/tree")
async def tree(owner: str, name: str, ref: str | None = None, db: Session = Depends(get_db),
               user: User = Depends(current_user)):
    repo, _ = await require_repo(db, user, owner, name)
    try:
        return {"ref": ref or repo.default_branch, "files": git_store().list_files(repo.id, ref or repo.default_branch)}
    except GitError as e:
        raise HTTPException(404, str(e))


@router.get("/repos/{owner}/{name}/file")
async def read_file(owner: str, name: str, path: str, ref: str | None = None, db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    repo, _ = await require_repo(db, user, owner, name)
    store = git_store()
    ref = ref or repo.default_branch
    try:
        content = store.read_file(repo.id, ref, path)
        sha = store.resolve(repo.id, ref)
    except (GitError, ValueError) as e:
        raise HTTPException(404, str(e))
    if content is None:
        raise HTTPException(404, "File not found")
    return {"path": path, "ref": ref, "commit": sha, "content": content}


class FileWrite(BaseModel):
    branch: str
    path: str
    content: str | None = None  # None deletes the file
    message: str = Field(default="", max_length=2000)
    expected_sha: str | None = None  # commit the editor was based on; prevents lost updates


@router.put("/repos/{owner}/{name}/file")
async def write_file(owner: str, name: str, body: FileWrite, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    repo, _ = await require_repo(db, user, owner, name, "write")
    pr = db.scalar(select(DocPullRequest).where(DocPullRequest.doc_repo_id == repo.id,
                                                DocPullRequest.head_branch == body.branch))
    if pr and pr.state != "open":
        raise HTTPException(409, "This pull request is no longer open")
    verb = "Delete" if body.content is None else "Update"
    try:
        sha = git_store().commit(repo.id, body.branch, {body.path: body.content},
                                 body.message.strip() or f"{verb} {body.path}", author_of(user),
                                 expected_sha=body.expected_sha)
    except RefMoved:
        raise HTTPException(409, "The branch changed since you started editing. Reload and try again.")
    except (GitError, ValueError) as e:
        raise HTTPException(400, str(e))
    telemetry.track("file_saved", user, on_pr_branch=pr is not None, deleted=body.content is None)
    if pr:  # a human edit on a doc PR changes its head, so approvals need to be refreshed
        from ..services.sync import refresh_check, schedule
        schedule(refresh_check(pr.id))
    return {"commit": sha}


@router.get("/repos/{owner}/{name}/commits")
async def commits(owner: str, name: str, ref: str | None = None, db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    repo, _ = await require_repo(db, user, owner, name)
    return [c.__dict__ for c in git_store().log(repo.id, ref or repo.default_branch, limit=100)]


# ------------------------------------------------------------------ members
class MemberAdd(BaseModel):
    login: str
    role: str = "write"


@router.get("/repos/{owner}/{name}/members")
async def members(owner: str, name: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Explicit members, plus everyone inherited from connected GitHub repos (with their GitHub permission)."""
    repo, _ = await require_repo(db, user, owner, name)
    explicit = [{"handle": repo.owner.handle, "avatar_url": repo.owner.avatar_url, "role": "owner", "source": "owner"}] + [
        {"handle": m.user.handle, "avatar_url": m.user.avatar_url, "role": m.role, "source": "added"} for m in repo.members]
    signed_in = {(u.host_id, u.github_id) for u in db.scalars(select(User))}
    inherited: dict[tuple[str, int], dict] = {}
    rank = {"read": 1, "write": 2, "admin": 3}
    for link in repo.links:
        for m in link.inherited:
            role = role_for(m.permission) or "read"
            key = (m.host_id, m.github_id)
            if key not in inherited or rank[role] > rank[inherited[key]["role"]]:
                inherited[key] = {"handle": m.login if m.host_id == "github" else f"{m.login}@{m.host_id}",
                                  "avatar_url": m.avatar_url, "role": role, "github_permission": m.permission,
                                  "source": link.gh_full_name, "signed_in": key in signed_in}
    return {"members": explicit,
            "inherited": sorted(inherited.values(), key=lambda m: (-rank[m["role"]], m["handle"].lower()))}


@router.post("/repos/{owner}/{name}/links/{link_id}/sync-members")
async def resync_members(owner: str, name: str, link_id: str, db: Session = Depends(get_db),
                         user: User = Depends(current_user)):
    repo, _ = await require_repo(db, user, owner, name, "admin")
    link = db.get(RepoLink, link_id)
    if not link or link.doc_repo_id != repo.id:
        raise HTTPException(404, "Link not found")
    try:
        return await sync_link_members(db, link, reason="manual")
    except GitHubError as e:
        raise HTTPException(502, f"Could not read the members of {link.gh_full_name} from GitHub: {e}")


@router.post("/repos/{owner}/{name}/members", status_code=201)
async def add_member(owner: str, name: str, body: MemberAdd, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    repo, _ = await require_repo(db, user, owner, name, "admin")
    if body.role not in ("read", "write", "admin"):
        raise HTTPException(422, "role must be read, write or admin")
    login, _, host = body.login.partition("@")
    target = db.scalar(select(User).where(User.login == login, User.host_id == (host or user.host_id)))
    if not target:
        raise HTTPException(404, "That user has not signed in to DocTheCode yet")
    m = db.scalar(select(RepoMember).where(RepoMember.repo_id == repo.id, RepoMember.user_id == target.id))
    if m:
        m.role = body.role
    else:
        db.add(RepoMember(repo_id=repo.id, user_id=target.id, role=body.role))
    db.commit()
    return {"handle": target.handle, "role": body.role}


@router.delete("/repos/{owner}/{name}/members/{handle}", status_code=204)
async def remove_member(owner: str, name: str, handle: str, db: Session = Depends(get_db),
                        user: User = Depends(current_user)):
    repo, _ = await require_repo(db, user, owner, name, "admin")
    for m in list(repo.members):
        if m.user.handle == handle:
            db.delete(m)
    db.commit()


# ------------------------------------------------------------------ GitHub links
@router.get("/github/repos")
async def github_repos(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Repos the user can connect: those where the DocTheCode app is installed and the user has access."""
    gh = github_client(user.host_id)
    token = await user_token(db, user)
    out = []
    try:
        for inst in await gh.user_installations(token):
            for r in await gh.user_installation_repos(token, inst["id"]):
                out.append({"id": r["id"], "full_name": r["full_name"], "private": r["private"],
                            "default_branch": r["default_branch"], "installation_id": inst["id"],
                            "can_admin": bool(r.get("permissions", {}).get("admin"))})
    except GitHubError as e:
        raise HTTPException(502, str(e))
    linked = {(l.host_id, l.gh_repo_id): l.doc_repo_id for l in db.scalars(select(RepoLink))}
    for r in out:
        r["linked_doc_repo_id"] = linked.get((user.host_id, r["id"]))
    return {"install_url": gh.install_url(), "repos": out}


class LinkCreate(BaseModel):
    gh_repo_id: int
    installation_id: int
    full_name: str
    require_check: bool = True  # add the DocTheCode check to branch protection on the default branch
    agent_instructions: str = ""


@router.post("/repos/{owner}/{name}/links", status_code=201)
async def create_link(owner: str, name: str, body: LinkCreate, db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    repo, _ = await require_repo(db, user, owner, name, "admin")
    gh = github_client(user.host_id)
    token = await user_token(db, user)
    gh_owner, gh_name = body.full_name.split("/", 1)
    perm = await gh.user_repo_permission(token, gh_owner, gh_name)
    if perm not in ("admin", "maintain", "write"):
        raise HTTPException(403, "You need write access to the GitHub repository to connect it")
    gh_repo = await gh.get_repo(token, gh_owner, gh_name)
    if gh_repo["id"] != body.gh_repo_id:
        raise HTTPException(400, "Repository id mismatch")
    if await gh.repo_installation_id(gh_owner, gh_name) is None:
        raise HTTPException(400, f"Install the DocTheCode GitHub App on {body.full_name} first: {gh.install_url()}")

    link = RepoLink(doc_repo_id=repo.id, host_id=user.host_id, gh_repo_id=body.gh_repo_id,
                    gh_full_name=gh_repo["full_name"], installation_id=body.installation_id,
                    agent_instructions=body.agent_instructions, created_by_id=user.id,
                    poll_cursor=time.time())  # PRs changed from now on get doc PRs
    db.add(link)
    try:
        db.commit()
    except IntegrityError:
        raise HTTPException(409, "That GitHub repository is already connected to a doc repo")

    warning = None
    try:  # everyone with access to the GitHub repo becomes a member right away
        await sync_link_members(db, link, reason="connected")
    except GitHubError as e:
        warning = (f"Connected, but could not import the members of {link.gh_full_name} yet ({e}). "
                   "DocTheCode retries every hour; use 'Re-sync members' to try now.")
    if body.require_check:
        try:
            await gh.require_check_on_branch(token, gh_owner, gh_name, gh_repo["default_branch"],
                                             get_settings().check_name)
        except GitHubError as e:
            warning = (warning + " " if warning else "") + (f"Connected, but could not make the check required on {gh_repo['default_branch']} "
                       f"({e}). A repo admin can add '{get_settings().check_name}' as a required status check.")
    telemetry.track("repo_linked", user, require_check=body.require_check, check_failed=warning is not None,
                    has_instructions=bool(body.agent_instructions.strip()))
    return {**link_json(link), "warning": warning}


class LinkUpdate(BaseModel):
    enabled: bool | None = None
    required_approvals: int | None = Field(default=None, ge=1, le=10)
    pass_when_no_changes: bool | None = None
    allow_author_approval: bool | None = None
    agent_instructions: str | None = None


@router.patch("/repos/{owner}/{name}/links/{link_id}")
async def update_link(owner: str, name: str, link_id: str, body: LinkUpdate, db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    repo, _ = await require_repo(db, user, owner, name, "admin")
    link = db.get(RepoLink, link_id)
    if not link or link.doc_repo_id != repo.id:
        raise HTTPException(404, "Link not found")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(link, k, v)
    db.commit()
    return link_json(link)


@router.delete("/repos/{owner}/{name}/links/{link_id}", status_code=204)
async def delete_link(owner: str, name: str, link_id: str, db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    repo, _ = await require_repo(db, user, owner, name, "admin")
    link = db.get(RepoLink, link_id)
    if link and link.doc_repo_id == repo.id:
        db.delete(link)
        db.commit()
        telemetry.track("repo_unlinked", user)


__all__ = ["router", "author_of", "ROLE_RANK"]
