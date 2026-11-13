"""Self-hosted git storage for documentation repos.

Every doc repo is a bare git repository on disk. All writes go through git plumbing
with a throwaway index, so there is never a working tree to keep clean and concurrent
writers are protected by compare-and-swap ref updates.

Because doc repos are plain git, exposing them for `git clone`/push (e.g. via
`git http-backend`) is a small follow-up; the MVP edits docs through the API.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

_SAFE_REPO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_SAFE_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
ZERO_SHA = "0" * 40


class GitError(RuntimeError):
    pass


class MergeConflict(GitError):
    def __init__(self, paths: list[str]):
        super().__init__("Merge conflict in: " + ", ".join(paths))
        self.paths = paths


class RefMoved(GitError):
    """The branch moved while we were writing (someone else committed)."""


@dataclass(frozen=True)
class Author:
    name: str
    email: str


@dataclass(frozen=True)
class CopyFrom:
    """In a commit's changes: give this path the same content as `path` in the parent commit."""
    path: str


@dataclass(frozen=True)
class Commit:
    sha: str
    author: str
    email: str
    timestamp: int
    message: str


@dataclass(frozen=True)
class ChangedFile:
    status: str  # A, M, D, R
    path: str


def validate_path(path: str) -> str:
    p = path.strip().lstrip("/")
    parts = p.split("/")
    if not p or any(part in ("", ".", "..") for part in parts) or parts[0] == ".git":
        raise ValueError(f"Invalid file path: {path!r}")
    if "\x00" in p or "\n" in p:
        raise ValueError(f"Invalid file path: {path!r}")
    return p


def validate_branch(name: str) -> str:
    if not _SAFE_BRANCH.match(name) or ".." in name or name.endswith((".lock", "/")) or "//" in name:
        raise ValueError(f"Invalid branch name: {name!r}")
    return name


class GitStore:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ helpers
    def path(self, repo_id: str) -> Path:
        if not _SAFE_REPO_ID.match(repo_id):
            raise ValueError(f"Invalid repo id: {repo_id!r}")
        return self.root / f"{repo_id}.git"

    def _git(self, repo_id: str, *args: str, input: bytes | None = None,
             env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
        full_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
        if env:
            full_env.update(env)
        proc = subprocess.run(
            ["git", f"--git-dir={self.path(repo_id)}", *args],
            input=input, capture_output=True, env=full_env,
        )
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args[:2])} failed: {proc.stderr.decode(errors='replace').strip()}")
        return proc

    def _out(self, repo_id: str, *args: str, **kw) -> str:
        return self._git(repo_id, *args, **kw).stdout.decode()

    @staticmethod
    def _author_env(author: Author) -> dict:
        return {
            "GIT_AUTHOR_NAME": author.name, "GIT_AUTHOR_EMAIL": author.email,
            "GIT_COMMITTER_NAME": "DocTheCode", "GIT_COMMITTER_EMAIL": "docthecode@localhost",
        }

    # --------------------------------------------------------------- lifecycle
    def exists(self, repo_id: str) -> bool:
        return self.path(repo_id).exists()

    def init_repo(self, repo_id: str, default_branch: str, author: Author,
                  initial_files: dict[str, str] | None = None) -> str:
        validate_branch(default_branch)
        p = self.path(repo_id)
        if p.exists():
            raise GitError(f"Repo {repo_id} already exists")
        subprocess.run(["git", "init", "-q", "--bare", f"--initial-branch={default_branch}", str(p)],
                       check=True, capture_output=True)
        self._git(repo_id, "config", "http.receivepack", "true")
        files = initial_files or {"README.md": f"# {repo_id}\n\nDocumentation lives here.\n"}
        return self.commit(repo_id, default_branch, files, "Initial commit", author, create=True)

    def delete_repo(self, repo_id: str) -> None:
        import shutil
        shutil.rmtree(self.path(repo_id), ignore_errors=True)

    # ---------------------------------------------------------------- branches
    def branch_sha(self, repo_id: str, branch: str) -> str | None:
        proc = self._git(repo_id, "rev-parse", "--verify", "--quiet", f"refs/heads/{validate_branch(branch)}^{{commit}}",
                         check=False)
        return proc.stdout.decode().strip() or None

    def resolve(self, repo_id: str, ref: str) -> str:
        if re.fullmatch(r"[0-9a-f]{40}", ref):
            return ref
        sha = self.branch_sha(repo_id, ref)
        if not sha:
            raise GitError(f"Unknown ref {ref!r}")
        return sha

    def list_branches(self, repo_id: str) -> dict[str, str]:
        out = self._out(repo_id, "for-each-ref", "--format=%(refname:strip=2) %(objectname)", "refs/heads")
        return dict(line.split(" ", 1) for line in out.splitlines() if line)

    def create_branch(self, repo_id: str, branch: str, from_ref: str) -> str:
        sha = self.resolve(repo_id, from_ref)
        self._git(repo_id, "update-ref", f"refs/heads/{validate_branch(branch)}", sha, ZERO_SHA)
        return sha

    def reset_branch(self, repo_id: str, branch: str, to_ref: str) -> str:
        sha = self.resolve(repo_id, to_ref)
        self._git(repo_id, "update-ref", f"refs/heads/{validate_branch(branch)}", sha)
        return sha

    def delete_branch(self, repo_id: str, branch: str) -> None:
        self._git(repo_id, "update-ref", "-d", f"refs/heads/{validate_branch(branch)}", check=False)

    # ------------------------------------------------------------------ reading
    def list_files(self, repo_id: str, ref: str, prefix: str = "") -> list[str]:
        sha = self.resolve(repo_id, ref)
        out = self._out(repo_id, "ls-tree", "-r", "-z", "--name-only", sha)
        files = [f for f in out.split("\x00") if f]
        if prefix:
            prefix = prefix.strip("/") + "/"
            files = [f for f in files if f.startswith(prefix)]
        return files

    def read_file(self, repo_id: str, ref: str, path: str) -> str | None:
        sha = self.resolve(repo_id, ref)
        proc = self._git(repo_id, "cat-file", "blob", f"{sha}:{validate_path(path)}", check=False)
        if proc.returncode != 0:
            return None
        return proc.stdout.decode("utf-8", errors="replace")

    def read_many(self, repo_id: str, ref: str, paths: list[str], max_bytes: int = 512_000) -> dict[str, str | None]:
        """Read many text files in one git process (for building page trees)."""
        if not paths:
            return {}
        sha = self.resolve(repo_id, ref)
        req = "".join(f"{sha}:{validate_path(p)}\n" for p in paths).encode()
        out = self._git(repo_id, "cat-file", "--batch", input=req).stdout
        result: dict[str, str | None] = {}
        pos = 0
        for p in paths:
            nl = out.index(b"\n", pos)
            header = out[pos:nl].decode()
            pos = nl + 1
            if header.endswith(" missing"):
                result[p] = None
                continue
            size = int(header.rsplit(" ", 1)[1])
            data = out[pos:pos + size]
            pos += size + 1  # trailing newline after each object
            result[p] = None if size > max_bytes else data.decode("utf-8", errors="replace")
        return result

    def log(self, repo_id: str, ref: str, limit: int = 50, since: str | None = None) -> list[Commit]:
        rng = f"{self.resolve(repo_id, since)}..{self.resolve(repo_id, ref)}" if since else self.resolve(repo_id, ref)
        out = self._out(repo_id, "log", f"-n{limit}", "--format=%H%x1f%an%x1f%ae%x1f%at%x1f%B%x1e", rng)
        commits = []
        for rec in out.split("\x1e"):
            rec = rec.strip("\n")
            if not rec:
                continue
            sha, an, ae, at, msg = rec.split("\x1f", 4)
            commits.append(Commit(sha, an, ae, int(at), msg.strip()))
        return commits

    def merge_base(self, repo_id: str, a: str, b: str) -> str:
        return self._out(repo_id, "merge-base", self.resolve(repo_id, a), self.resolve(repo_id, b)).strip()

    def changed_files(self, repo_id: str, base: str, head: str) -> list[ChangedFile]:
        """Files changed on `head` since it forked from `base` (three-dot semantics)."""
        mb = self.merge_base(repo_id, base, head)
        out = self._out(repo_id, "diff", "--name-status", "-z", "-M", mb, self.resolve(repo_id, head))
        parts = [p for p in out.split("\x00") if p]
        result, i = [], 0
        while i < len(parts):
            status = parts[i][0]
            if status in "RC":
                result.append(ChangedFile("R", parts[i + 2]))
                i += 3
            else:
                result.append(ChangedFile(status, parts[i + 1]))
                i += 2
        return result

    def diff(self, repo_id: str, base: str, head: str) -> str:
        mb = self.merge_base(repo_id, base, head)
        return self._out(repo_id, "diff", "-M", "--no-color", mb, self.resolve(repo_id, head))

    # ------------------------------------------------------------------ writing
    def commit(self, repo_id: str, branch: str, changes: dict[str, "str | CopyFrom | None"], message: str,
               author: Author, expected_sha: str | None = None, create: bool = False) -> str:
        """Commit `changes` (path -> new content, or None to delete) onto `branch`.

        Uses a temporary index so no working tree is needed. The ref update is a
        compare-and-swap against the parent we built on; RefMoved is raised if the
        branch moved underneath us.
        """
        validate_branch(branch)
        parent = None if create else self.branch_sha(repo_id, branch)
        if not create and parent is None:
            raise GitError(f"Branch {branch!r} does not exist")
        if expected_sha and parent != expected_sha:
            raise RefMoved(f"{branch} is at {parent}, expected {expected_sha}")

        with tempfile.TemporaryDirectory() as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index"), **self._author_env(author)}
            if parent:
                self._git(repo_id, "read-tree", parent, env=env)
            entries = []
            for raw_path, content in changes.items():
                path = validate_path(raw_path)
                if content is None:
                    entries.append(f"0 {ZERO_SHA}\t{path}")
                elif isinstance(content, CopyFrom):
                    if not parent:
                        raise GitError("Nothing to copy from in a new branch")
                    src = self._git(repo_id, "rev-parse", "--verify", "--quiet",
                                    f"{parent}:{validate_path(content.path)}", check=False).stdout.decode().strip()
                    if not src:
                        raise GitError(f"{content.path} does not exist")
                    entries.append(f"100644 {src}\t{path}")
                else:
                    blob = self._out(repo_id, "hash-object", "-w", "--stdin", input=content.encode()).strip()
                    entries.append(f"100644 {blob}\t{path}")
            if entries:
                self._git(repo_id, "update-index", "-z", "--index-info",
                          input=("\x00".join(entries) + "\x00").encode(), env=env)
            tree = self._out(repo_id, "write-tree", env=env).strip()

        if parent and tree == self._out(repo_id, "rev-parse", f"{parent}^{{tree}}").strip():
            return parent  # nothing changed

        args = ["commit-tree", tree, "-m", message] + (["-p", parent] if parent else [])
        sha = self._out(repo_id, *args, env=self._author_env(author)).strip()
        proc = self._git(repo_id, "update-ref", f"refs/heads/{branch}", sha, parent or ZERO_SHA, check=False)
        if proc.returncode != 0:
            raise RefMoved(f"{branch} moved during commit")
        return sha

    def can_fast_forward(self, repo_id: str, base: str, head: str) -> bool:
        return self.merge_base(repo_id, base, head) == self.resolve(repo_id, base)

    def merge(self, repo_id: str, base_branch: str, head_branch: str, message: str,
              author: Author, squash: bool = False) -> str:
        """Merge head into base. Raises MergeConflict if the trees conflict."""
        base_sha = self.resolve(repo_id, base_branch)
        head_sha = self.resolve(repo_id, head_branch)
        proc = self._git(repo_id, "merge-tree", "--write-tree", "--name-only", "-z", base_sha, head_sha, check=False)
        out = proc.stdout.decode().split("\x00")
        if proc.returncode == 1:
            # Output: <tree>\0<conflicted path>\0...\0\0<informational messages>
            conflicted = []
            for p in out[1:]:
                if not p:
                    break
                conflicted.append(p)
            raise MergeConflict(sorted(set(conflicted)) or ["<unknown>"])
        if proc.returncode != 0:
            raise GitError(proc.stderr.decode())
        tree = out[0].strip()
        parents = ["-p", base_sha] if squash else ["-p", base_sha, "-p", head_sha]
        sha = self._out(repo_id, "commit-tree", tree, *parents, "-m", message, env=self._author_env(author)).strip()
        if self._git(repo_id, "update-ref", f"refs/heads/{base_branch}", sha, base_sha, check=False).returncode:
            raise RefMoved(f"{base_branch} moved during merge")
        return sha
