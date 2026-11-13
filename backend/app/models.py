from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("host_id", "github_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    host_id: Mapped[str] = mapped_column(String(64))          # which GitHub host they signed in with
    github_id: Mapped[int] = mapped_column(BigInteger)
    login: Mapped[str] = mapped_column(String(100))
    name: Mapped[str | None] = mapped_column(String(300))
    email: Mapped[str | None] = mapped_column(String(320))
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)  # can manage DocTheCode settings
    access_token_enc: Mapped[str] = mapped_column(Text)        # encrypted with DOCTHECODE_SECRET_KEY
    refresh_token_enc: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[float | None] = mapped_column(Float)
    access_synced_at: Mapped[float | None] = mapped_column(Float)  # last GitHub repo-access sync
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    @property
    def handle(self) -> str:
        """Globally unique handle: plain login on github.com, login@host for enterprise hosts."""
        return self.login if self.host_id == "github" else f"{self.login}@{self.host_id}"


class DocRepo(Base):
    __tablename__ = "doc_repos"
    __table_args__ = (UniqueConstraint("owner_id", "name"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)  # also the on-disk git id
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    default_branch: Mapped[str] = mapped_column(String(100), default="main")
    next_pr_number: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    owner: Mapped[User] = relationship()
    members: Mapped[list["RepoMember"]] = relationship(back_populates="repo", cascade="all, delete-orphan")
    links: Mapped[list["RepoLink"]] = relationship(back_populates="doc_repo", cascade="all, delete-orphan")


class RepoMember(Base):
    __tablename__ = "repo_members"
    __table_args__ = (UniqueConstraint("repo_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo_id: Mapped[str] = mapped_column(ForeignKey("doc_repos.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    role: Mapped[str] = mapped_column(String(16), default="write")  # admin | write | read

    repo: Mapped[DocRepo] = relationship(back_populates="members")
    user: Mapped[User] = relationship()


class RepoLink(Base):
    """A doc repo connected to a GitHub code repo."""
    __tablename__ = "repo_links"
    __table_args__ = (UniqueConstraint("host_id", "gh_repo_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    doc_repo_id: Mapped[str] = mapped_column(ForeignKey("doc_repos.id", ondelete="CASCADE"))
    host_id: Mapped[str] = mapped_column(String(64))
    gh_repo_id: Mapped[int] = mapped_column(BigInteger)
    gh_full_name: Mapped[str] = mapped_column(String(200))   # owner/name, refreshed from webhooks
    installation_id: Mapped[int] = mapped_column(BigInteger)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    required_approvals: Mapped[int] = mapped_column(Integer, default=1)
    pass_when_no_changes: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_author_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    # Code-repo users with at least this permission may approve doc PRs (besides doc repo members).
    approver_min_permission: Mapped[str] = mapped_column(String(16), default="write")
    agent_instructions: Mapped[str] = mapped_column(Text, default="")
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    members_synced_at: Mapped[float | None] = mapped_column(Float)
    members_sync_error: Mapped[str | None] = mapped_column(Text)
    # "Check GitHub for changes" (works without inbound webhooks): last seen PR update, list ETag, last check
    poll_cursor: Mapped[float | None] = mapped_column(Float)
    poll_etag: Mapped[str | None] = mapped_column(String(200))
    polled_at: Mapped[float | None] = mapped_column(Float)
    poll_error: Mapped[str | None] = mapped_column(Text)

    doc_repo: Mapped[DocRepo] = relationship(back_populates="links")
    inherited: Mapped[list["InheritedMember"]] = relationship(back_populates="link", cascade="all, delete-orphan")

    @property
    def owner(self) -> str:
        return self.gh_full_name.split("/", 1)[0]

    @property
    def repo(self) -> str:
        return self.gh_full_name.split("/", 1)[1]


class DocPullRequest(Base):
    __tablename__ = "doc_pull_requests"
    __table_args__ = (UniqueConstraint("doc_repo_id", "number"), UniqueConstraint("link_id", "gh_pr_number"))

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    doc_repo_id: Mapped[str] = mapped_column(ForeignKey("doc_repos.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    author_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))  # None = opened by DocTheCode
    head_branch: Mapped[str] = mapped_column(String(200))
    base_branch: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(16), default="open")  # open | merged | closed
    merged_sha: Mapped[str | None] = mapped_column(String(40))

    # Mirror of the GitHub code PR (null for doc PRs opened by hand in DocTheCode)
    link_id: Mapped[str | None] = mapped_column(ForeignKey("repo_links.id", ondelete="SET NULL"), index=True)
    gh_pr_number: Mapped[int | None] = mapped_column(Integer)
    gh_pr_url: Mapped[str | None] = mapped_column(String(500))
    gh_pr_author: Mapped[str | None] = mapped_column(String(100))
    gh_pr_state: Mapped[str | None] = mapped_column(String(16))  # open | closed | merged
    gh_head_sha: Mapped[str | None] = mapped_column(String(40))
    gh_base_ref: Mapped[str | None] = mapped_column(String(200))
    gh_check_run_id: Mapped[int | None] = mapped_column(BigInteger)
    gh_check_run_sha: Mapped[str | None] = mapped_column(String(40))
    gh_comment_id: Mapped[int | None] = mapped_column(BigInteger)

    agent_status: Mapped[str] = mapped_column(String(16), default="idle")
    agent_processed_sha: Mapped[str | None] = mapped_column(String(40))  # last code sha the agent handled
    agent_summary: Mapped[str] = mapped_column(Text, default="")
    agent_log: Mapped[list] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    doc_repo: Mapped[DocRepo] = relationship()
    author: Mapped[User | None] = relationship()
    link: Mapped[RepoLink | None] = relationship()
    reviews: Mapped[list["Review"]] = relationship(back_populates="pr", cascade="all, delete-orphan",
                                                   order_by="Review.created_at")


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pr_id: Mapped[str] = mapped_column(ForeignKey("doc_pull_requests.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    state: Mapped[str] = mapped_column(String(24))  # approved | changes_requested | commented
    body: Mapped[str] = mapped_column(Text, default="")
    doc_head_sha: Mapped[str] = mapped_column(String(40))
    addressed_by_agent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    pr: Mapped[DocPullRequest] = relationship(back_populates="reviews")
    user: Mapped[User] = relationship()


# --------------------------------------------------------------------------- settings & secrets
class GitHubHostConfig(Base):
    """A GitHub App registration for one GitHub host (github.com or a GHE Server).

    Secrets are encrypted with DOCTHECODE_SECRET_KEY; that key never touches the database.
    """
    __tablename__ = "github_hosts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)   # slug used in URLs, e.g. "github"
    name: Mapped[str] = mapped_column(String(100))
    web_url: Mapped[str] = mapped_column(String(300))
    api_url: Mapped[str] = mapped_column(String(300))
    app_id: Mapped[str] = mapped_column(String(32))
    app_slug: Mapped[str] = mapped_column(String(100))
    client_id: Mapped[str] = mapped_column(String(100))
    client_secret_enc: Mapped[str] = mapped_column(Text)
    private_key_enc: Mapped[str] = mapped_column(Text)
    webhook_secret_enc: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class AppSetting(Base):
    """Key/value settings. Secret values are stored encrypted."""
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class UserRepoAccess(Base):
    """GitHub repos a user can see (refreshed from GitHub on sign-in and periodically)."""
    __tablename__ = "user_repo_access"
    __table_args__ = (UniqueConstraint("user_id", "host_id", "gh_repo_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    host_id: Mapped[str] = mapped_column(String(64))
    gh_repo_id: Mapped[int] = mapped_column(BigInteger, index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    permission: Mapped[str] = mapped_column(String(16))  # admin | maintain | write | triage | read
    installation_id: Mapped[int] = mapped_column(BigInteger)


class DocRepoBlob(Base):
    """The git contents of a doc repo, as a bundle. The database is the source of truth."""
    __tablename__ = "doc_repo_blobs"

    repo_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class InheritedMember(Base):
    """Someone with access to a connected GitHub repo, and so to its doc repo.

    Keyed by GitHub account id (not DocTheCode user) so people have access from their very first
    sign-in, without an invite. Kept in sync with GitHub; removed when they lose access there.
    """
    __tablename__ = "inherited_members"
    __table_args__ = (UniqueConstraint("link_id", "github_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    link_id: Mapped[str] = mapped_column(ForeignKey("repo_links.id", ondelete="CASCADE"), index=True)
    host_id: Mapped[str] = mapped_column(String(64))
    github_id: Mapped[int] = mapped_column(BigInteger, index=True)
    login: Mapped[str] = mapped_column(String(100))
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    permission: Mapped[str] = mapped_column(String(16))  # admin | maintain | write | triage | read

    link: Mapped[RepoLink] = relationship(back_populates="inherited")
