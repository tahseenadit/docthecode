"""Read-only access to the code repository for the documentation agent.

The agent needs more than the pull request's diff: to write business, tutorial, reference and
technical documentation it has to understand the whole codebase. DocTheCode downloads one
snapshot of the repo at the PR's head commit (GitHub's tarball endpoint), keeps the text files in
memory for the duration of the run, and offers the agent four operations on it: an overview,
listing, reading (by line range) and searching.

If the snapshot can't be downloaded (too big, or the endpoint is unavailable on an old GitHub
Enterprise Server), `ApiCodebase` falls back to the GitHub contents API: listing and reading still
work, searching is limited to file paths.

Pure standard library, so it is easy to test.
"""
from __future__ import annotations

import fnmatch
import io
import posixpath
import re
import tarfile
from collections import Counter
from typing import Awaitable, Callable, Protocol

MAX_FILE_BYTES = 400_000          # bigger files are almost always generated or data
MAX_TOTAL_BYTES = 60_000_000      # text kept in memory per run
MAX_FILES = 25_000
MAX_READ_CHARS = 60_000
MAX_SEARCH_HITS = 150

SKIP_DIRS = {
    ".git", "node_modules", "vendor", "third_party", "third-party", "bower_components", "dist", "build",
    "target", "obj", ".next", ".nuxt", ".svelte-kit", ".venv", "venv", "__pycache__", ".tox",
    ".mypy_cache", ".pytest_cache", ".gradle", ".idea", ".vscode", "coverage", ".terraform", "Pods",
    "DerivedData", ".dart_tool", ".cache", "site-packages",
}
SKIP_SUFFIXES = (
    ".min.js", ".min.css", ".map", ".lock", "lock.json", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
    ".pdf", ".zip", ".gz", ".tgz", ".jar", ".war", ".class", ".so", ".dll", ".dylib", ".exe", ".bin", ".woff",
    ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".mov", ".wav", ".pyc", ".parquet", ".sqlite", ".db",
)
LOCKFILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Cargo.lock", "Gemfile.lock",
             "composer.lock", "go.sum", "uv.lock", "Pipfile.lock", "bun.lockb"}

LANGUAGES = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin",
    ".kts": "Kotlin", ".scala": "Scala", ".cs": "C#", ".fs": "F#", ".rb": "Ruby", ".php": "PHP", ".swift": "Swift",
    ".m": "Objective-C", ".c": "C", ".h": "C/C++ header", ".cc": "C++", ".cpp": "C++", ".hpp": "C++",
    ".dart": "Dart", ".ex": "Elixir", ".exs": "Elixir", ".erl": "Erlang", ".clj": "Clojure", ".lua": "Lua",
    ".r": "R", ".jl": "Julia", ".sql": "SQL", ".sh": "Shell", ".ps1": "PowerShell", ".vue": "Vue",
    ".svelte": "Svelte", ".html": "HTML", ".css": "CSS", ".scss": "SCSS", ".proto": "Protocol Buffers",
    ".graphql": "GraphQL", ".gql": "GraphQL", ".tf": "Terraform", ".yaml": "YAML", ".yml": "YAML",
    ".json": "JSON", ".toml": "TOML", ".md": "Markdown", ".rst": "reStructuredText",
}

# Where each kind of documentation usually finds its facts. Matched against path segments and file names.
HINTS: dict[str, tuple[str, ...]] = {
    "business": ("domain", "domains", "model", "models", "entities", "entity", "services", "service", "usecases",
                 "use_cases", "use-cases", "rules", "policies", "policy", "workflows", "workflow", "billing",
                 "pricing", "payments", "orders", "accounts", "permissions", "roles", "subscriptions", "features",
                 "core", "logic", "validators", "validation"),
    "reference": ("api", "apis", "routes", "router", "routers", "controllers", "handlers", "endpoints", "cli",
                  "cmd", "commands", "config", "configuration", "settings", "schema", "schemas", "openapi",
                  "swagger", "graphql", "proto", "events", "webhooks", "sdk", "public"),
    "technical": ("infra", "infrastructure", "deploy", "deployment", "k8s", "kubernetes", "helm", "terraform",
                  "docker", "migrations", "db", "database", "storage", "queue", "queues", "jobs", "workers",
                  "adapters", "integrations", "clients", "middleware", "auth", "security", "cache", ".github"),
}
KEY_FILES = (
    "README*", "readme*", "CONTRIBUTING*", "CHANGELOG*", "ARCHITECTURE*", "package.json", "pyproject.toml",
    "setup.py", "setup.cfg", "requirements*.txt", "go.mod", "Cargo.toml", "pom.xml", "build.gradle*",
    "Gemfile", "composer.json", "*.csproj", "*.sln", "mix.exs", "pubspec.yaml", "Dockerfile*", "docker-compose*",
    "compose.y*ml", "Makefile", "Procfile", "*.env.example", ".env.example", "env.example", "openapi*.y*ml",
    "openapi*.json", "swagger*", "*.proto", "*.graphql", "schema.prisma", "render.yaml", "app.yaml",
    "serverless.y*ml", "vercel.json", "netlify.toml", "fly.toml", "main.py", "app.py", "manage.py", "main.go",
    "main.rs", "index.ts", "index.js", "server.ts", "server.js", "Program.cs", "Application.java",
)


class Codebase(Protocol):
    """What the agent's code tools need. Every method returns text for the model."""
    async def overview(self) -> str: ...
    async def list(self, prefix: str = "", depth: int | None = None) -> str: ...
    async def read(self, path: str, start: int | None = None, end: int | None = None) -> str | None: ...
    async def search(self, pattern: str, prefix: str = "", glob: str = "") -> str: ...


def _norm(path: str) -> str:
    path = posixpath.normpath("/" + (path or "").strip()).lstrip("/")
    return "" if path == "." else path


def _skipped(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    return (any(p in SKIP_DIRS for p in parts[:-1]) or name in LOCKFILES
            or name.lower().endswith(SKIP_SUFFIXES))


def _numbered(lines: list[str], first: int) -> str:
    width = len(str(first + len(lines) - 1))
    return "\n".join(f"{str(first + i).rjust(width)}  {line}" for i, line in enumerate(lines))


def _read_text(files: dict[str, str], path: str, start: int | None, end: int | None) -> str | None:
    path = _norm(path)
    if path not in files:
        return None
    text = files[path]
    if start is None and end is None:
        if len(text) <= MAX_READ_CHARS:
            return text
        lines = text.splitlines()
        shown, size = [], 0
        for line in lines:
            if size + len(line) + 1 > MAX_READ_CHARS:
                break
            shown.append(line)
            size += len(line) + 1
        return (_numbered(shown, 1) + f"\n\n[... file continues: {len(lines)} lines in total. Read the rest with "
                f"start_line={len(shown) + 1}.]")
    lines = text.splitlines()
    s = max(1, int(start or 1))
    e = min(len(lines), int(end or len(lines)), s + 1500)
    if s > len(lines):
        return f"(the file has only {len(lines)} lines)"
    return _numbered(lines[s - 1:e], s) + (f"\n[... {len(lines) - e} more lines]" if e < len(lines) else "")


def _list_text(paths: list[str], prefix: str, depth: int | None) -> str:
    prefix = _norm(prefix)
    pre = prefix + "/" if prefix else ""
    inside = [p for p in paths if p.startswith(pre) or p == prefix]
    if not inside:
        return "(no files)"
    if depth is None and len(inside) <= 400:
        return "\n".join(inside)
    depth = depth or 2
    counts: Counter[str] = Counter()
    for p in inside:
        rest = p[len(pre):].split("/")
        key = "/".join(rest[:depth]) + ("/" if len(rest) > depth else "")
        counts[pre + key] += 1
    lines = [f"{k}  ({n} files)" if k.endswith("/") else k for k, n in sorted(counts.items())]
    note = f"{len(inside)} files under '{prefix or '/'}', grouped to depth {depth}. List a folder to see its files."
    return note + "\n" + "\n".join(lines[:1500])


def build_overview(paths: list[str], sizes: dict[str, int], skipped: int = 0, note: str = "") -> str:
    """A compact map of the repository: size, languages, top-level layout, key files and where to look."""
    langs: Counter[str] = Counter()
    for p in paths:
        ext = posixpath.splitext(p)[1].lower()
        if ext in LANGUAGES:
            langs[LANGUAGES[ext]] += 1
    top: Counter[str] = Counter()
    for p in paths:
        parts = p.split("/")
        top[parts[0] + ("/" if len(parts) > 1 else "")] += 1
    second: dict[str, Counter[str]] = {}
    for p in paths:
        parts = p.split("/")
        if len(parts) > 2:
            second.setdefault(parts[0], Counter())[parts[1] + "/"] += 1
    key = sorted(p for p in paths if p.count("/") <= 2 and any(fnmatch.fnmatch(posixpath.basename(p), k) for k in KEY_FILES))
    hints: dict[str, list[str]] = {k: [] for k in HINTS}
    dirs = sorted({posixpath.dirname(p) for p in paths if "/" in p})
    for d in dirs:
        segs = {s.lower() for s in d.split("/")}
        if segs & {"test", "tests", "spec", "specs", "__tests__", "fixtures", "examples", "e2e"}:
            continue
        hidden = any(s.startswith(".") for s in segs)
        for kind, words in HINTS.items():
            if hidden and kind != "technical":   # .github/workflows is CI, not a business workflow
                continue
            if segs & set(words) and len(hints[kind]) < 25 and not any(d.startswith(h + "/") for h in hints[kind]):
                hints[kind].append(d)

    out = [f"{len(paths)} text files ({sum(sizes.get(p, 0) for p in paths) // 1024} KB)"
           + (f"; {skipped} generated, vendored, binary or very large files left out" if skipped else "") + "."]
    if note:
        out.append(note)
    if langs:
        out.append("Languages (files): " + ", ".join(f"{k} {v}" for k, v in langs.most_common(12)))
    out.append("Top-level layout:")
    for name, n in sorted(top.items()):
        sub = second.get(name.rstrip("/"))
        detail = ""
        if sub:
            detail = " → " + ", ".join(f"{s} ({c})" for s, c in sub.most_common(8))
        out.append(f"  {name}  ({n} files){detail}" if name.endswith("/") else f"  {name}")
    if key:
        out.append("Key files: " + ", ".join(key[:60]))
    labels = {"business": "Business rules and domain logic", "reference": "Interfaces to document as reference",
              "technical": "Architecture and operations"}
    for kind, found in hints.items():
        if found:
            out.append(f"Likely sources for {labels[kind]}: " + ", ".join(found))
    return "\n".join(out)


def _search_text(files: dict[str, str], pattern: str, prefix: str, glob: str) -> str:
    try:
        rx = re.compile(pattern, re.I | re.M)
    except re.error:
        rx = re.compile(re.escape(pattern), re.I | re.M)
    pre = _norm(prefix)
    hits, files_hit, total = [], 0, 0
    for path in sorted(files):
        if pre and not (path == pre or path.startswith(pre + "/")):
            continue
        if glob and not (fnmatch.fnmatch(path, glob) or fnmatch.fnmatch(posixpath.basename(path), glob)):
            continue
        text = files[path]
        if not rx.search(text):
            continue
        files_hit += 1
        for no, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                total += 1
                if len(hits) < MAX_SEARCH_HITS:
                    hits.append(f"{path}:{no}: {line.strip()[:200]}")
    if not hits:
        return "(no matches)"
    more = f"\n[... {total - len(hits)} more matches; narrow the search with prefix or glob]" if total > len(hits) else ""
    return f"{total} matches in {files_hit} files\n" + "\n".join(hits) + more


class SnapshotCodebase:
    """The repo's text files at one commit, held in memory."""

    def __init__(self, files: dict[str, str], skipped: int = 0, note: str = ""):
        self.files, self.skipped, self.note = files, skipped, note

    @classmethod
    def from_tarball(cls, data: bytes) -> "SnapshotCodebase":
        """Build from a GitHub tarball (every path starts with one '<owner>-<repo>-<sha>/' folder)."""
        files: dict[str, str] = {}
        skipped = total = 0
        truncated = False
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            for m in tar:
                if not m.isfile():
                    continue
                path = _norm(m.name.split("/", 1)[1] if "/" in m.name else m.name)
                if not path or _skipped(path) or m.size > MAX_FILE_BYTES:
                    skipped += 1
                    continue
                if len(files) >= MAX_FILES or total + m.size > MAX_TOTAL_BYTES:
                    skipped += 1
                    truncated = True
                    continue
                f = tar.extractfile(m)
                raw = f.read() if f else b""
                if b"\0" in raw[:8192]:
                    skipped += 1
                    continue
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = raw.decode("latin-1")
                files[path] = text
                total += len(raw)
        note = "The repository is larger than DocTheCode loads at once; some files are missing." if truncated else ""
        return cls(files, skipped, note)

    async def overview(self) -> str:
        return build_overview(sorted(self.files), {p: len(t) for p, t in self.files.items()}, self.skipped, self.note)

    async def list(self, prefix: str = "", depth: int | None = None) -> str:
        return _list_text(sorted(self.files), prefix, depth)

    async def read(self, path: str, start: int | None = None, end: int | None = None) -> str | None:
        return _read_text(self.files, path, start, end)

    async def search(self, pattern: str, prefix: str = "", glob: str = "") -> str:
        return _search_text(self.files, pattern, prefix, glob)


class ApiCodebase:
    """Fallback when no snapshot is available: lists and reads files one by one through the GitHub API."""

    def __init__(self, list_paths: Callable[[], Awaitable[list[str]]],
                 read_file: Callable[[str], Awaitable[str | None]]):
        self._list_paths, self._read_file = list_paths, read_file
        self._paths: list[str] | None = None
        self._cache: dict[str, str | None] = {}

    async def _all(self) -> list[str]:
        if self._paths is None:
            self._paths = sorted(p for p in await self._list_paths() if not _skipped(p))
        return self._paths

    async def overview(self) -> str:
        paths = await self._all()
        return build_overview(paths, {}, 0, "Full-text search is not available for this repository: "
                              "search_code only matches file paths. Read files to see their contents.")

    async def list(self, prefix: str = "", depth: int | None = None) -> str:
        return _list_text(await self._all(), prefix, depth)

    async def read(self, path: str, start: int | None = None, end: int | None = None) -> str | None:
        path = _norm(path)
        if path not in self._cache:
            self._cache[path] = await self._read_file(path)
        text = self._cache[path]
        return None if text is None else _read_text({path: text}, path, start, end)

    async def search(self, pattern: str, prefix: str = "", glob: str = "") -> str:
        # Search the files already read in full, plus file paths.
        loaded = {p: t for p, t in self._cache.items() if t is not None}
        content = _search_text(loaded, pattern, prefix, glob) if loaded else "(no files read yet)"
        try:
            rx = re.compile(pattern, re.I)
        except re.error:
            rx = re.compile(re.escape(pattern), re.I)
        names = [p for p in await self._all() if rx.search(p)][:100]
        return ("Matches in files read so far:\n" + content + "\n\nMatching file paths:\n"
                + ("\n".join(names) or "(none)"))
