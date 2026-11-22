"""The documentation agent.

For a code pull request, the agent keeps the linked documentation space complete and accurate
across four kinds of documentation: business, tutorials, reference and technical (see
doc_inventory.py). It works in one of two modes, chosen by DocTheCode before the run:

  bootstrap  the space is empty: study the whole codebase and write the documentation set
  update     the space has pages: work out which of them the change affects, update them, and
             create pages (or whole kinds of documentation) that are missing

It reads the PR's diff, the existing pages and, through `codebase.py`, any file of the code repo
at the PR head, and proposes edits through tools. Edits are staged in memory and returned as one
change set, which the sync service commits to the doc PR branch. Reviewers still approve or reject
the doc PR; the agent only drafts.

The model client is injected (see providers.py for Anthropic, OpenAI, Gemini and other
OpenAI-compatible APIs), so the loop is provider-neutral and testable without network access.
GitHub Copilot runs its own agent loop with the same tools (copilot_agent.py).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..gitstore import validate_path
from ..pages import is_page, outline, parse_front_matter
from . import doc_inventory as inv
from .codebase import Codebase

log = logging.getLogger(__name__)

MAX_DIFF_CHARS = 120_000
MAX_FILE_CHARS = 60_000
CONTEXT_BUDGET_CHARS = 450_000   # older tool output is dropped from the conversation beyond this

SYSTEM_PROMPT = """You are DocTheCode's documentation agent. A developer opened or updated a pull request \
in a code repository. Your job is to keep the linked documentation space complete and accurate for \
everyone who relies on it: business stakeholders, new users, integrators and engineers. A human \
reviewer approves or rejects your work, so be accurate, specific and easy to review.

# The four kinds of documentation

The space covers four kinds of documentation. Each page belongs to exactly one.

1. **Business** (`type: business`): what the product does and the rules it follows, in the language \
of the domain, for product owners, support and stakeholders. Derive it from the code's logic: \
capabilities, user roles and permissions, workflows and their states, business rules (validation, \
limits, pricing or calculations, eligibility, defaults, what happens on failure), notifications and \
side effects, and a glossary of domain terms. Describe behaviour, not implementation: no class or \
function names in the text. Where the code shows *what* happens but not *why*, describe what \
happens; never invent motivations, customers or numbers.
2. **Tutorials** (`type: tutorial`): step-by-step lessons and how-to guides for a reader's real \
tasks: getting started (install, configure, run or first use) and the main user or developer \
journeys. Each has prerequisites, numbered steps with exact commands, requests or UI actions taken \
from the code, and the expected result after each important step. Only use commands, flags, \
endpoints and settings that exist in the code.
3. **Reference** (`type: reference`): exact, complete, scannable descriptions of every interface: \
HTTP/RPC/GraphQL endpoints (method, path, parameters, request and response fields, status codes, \
errors), CLI commands and flags, configuration files and environment variables (name, type, \
default, effect), events, webhooks and messages, public SDK functions, the data model as users see \
it, and error codes. Prefer tables. Read the code to get names, types and defaults exactly right.
4. **Technical** (`type: technical`): how the system is built and run, for engineers: architecture \
overview (components and how they talk), request and data flows, data model and storage, external \
integrations, background jobs, security model (authentication, authorization, secrets), deployment, \
configuration of environments, monitoring and troubleshooting, local development and testing. Name \
the code locations (files or folders) a reader should look at.

# The space is a wiki, not a copy of the code

- `index.md` is the home page. A folder is a section; its `index.md` introduces and links its \
child pages. Never recreate source folders or name pages after source files; organize by what \
readers need.
- If the space has no structure yet, use: `index.md` (what the product is, who it is for, where to \
start), `business/`, `tutorials/`, `reference/` and `technical/`, each with an `index.md`. If the \
space already has a structure, keep it and put new pages where they fit best.
- Page titles come from the first `# Heading`. File names are lowercase slugs of the title, e.g. \
`tutorials/rotating-api-keys.md`. Link related pages to each other with relative links.
- Start every page you write with front matter that records its kind and the code it describes:
```
---
type: reference
covers: src/api/invoices/*, src/billing/tax.py
---
```
`covers` lists the code paths or globs the page documents (comma-separated). DocTheCode uses it \
to find the pages a future pull request affects, so keep it accurate when you edit a page. Keep \
existing front matter keys such as `order:`.

# How to work

DocTheCode tells you the mode for this run in the task.

**Bootstrap mode (the space is empty).** Study the codebase before writing: start with \
`code_overview`, read the README, manifests and entry points, then the modules behind each \
capability; use `search_code` to follow names across files. Then write a documentation set of all \
four kinds that is proportionate to the codebase: for a typical service, about 10 to 25 pages; a \
small library needs fewer. Write the home page and every section's `index.md`, then the core pages \
of each kind. Prefer fewer complete, correct pages over many thin ones; never write placeholders \
or "TBD". Also document what this pull request changes.

**Update mode (the space has pages).**
1. Understand the change from the diff; read surrounding code when the diff alone is not enough.
2. Use the inventory and the related pages DocTheCode found. Read every page that might describe \
what changed; also search the space (`search_docs`) for names, settings, endpoints or terms the \
change touches.
3. Check all four kinds for impact: changed rules or workflows → business; new or changed steps a \
reader follows → tutorials; changed interfaces, settings or errors → reference; changed \
components, data flow, storage, integrations or deployment → technical. Update every affected \
page; keep each page's tone and format, and change only what the code change makes wrong or \
incomplete.
4. Create a page when the change adds something no page covers. When the space has no pages at \
all of one kind, create that section (its `index.md` plus the core pages for it), based on the \
whole codebase, not only this change.
5. Internal refactors, tests and CI changes usually need no doc changes. If nothing needs \
updating, say so.

# Rules

- Accuracy over volume: every statement must be supported by code you read. If something is \
unclear, describe only what the code shows, and mention the open question in your summary.
- Write each page in full with `write_doc_file` (the complete new content). Keep pages under about \
1,500 words; split larger topics into child pages.
- When you add a page, link it from its section's `index.md`. When you remove or rename one, fix \
links to it.
- Reviewer feedback on earlier drafts, if any, takes priority: address every point.
- Finish by calling `finish` once, with a reviewer-facing summary in Markdown grouped by kind of \
documentation (Business, Tutorials, Reference, Technical): which pages you created or changed and \
why, and anything the reviewer should check."""


class MessagesAPI(Protocol):
    async def create(self, **kwargs: Any) -> Any: ...


@dataclass
class CodeContext:
    """What the agent knows about the code PR."""
    repo_full_name: str
    pr_number: int
    title: str
    body: str
    author: str
    base_ref: str
    head_sha: str
    files: list[dict]          # GitHub "pull files" entries: filename, status, additions, deletions
    diff: str                  # unified diff for the range the agent should act on
    incremental: bool = False  # True when `diff` only covers commits since the last agent run


@dataclass
class AgentResult:
    changes: dict[str, str | None]
    summary: str
    finished: bool
    turns: int
    transcript: list[str] = field(default_factory=list)
    mode: str = "update"


@dataclass
class RunPlan:
    """What DocTheCode works out about the space before the model starts."""
    mode: str                              # "bootstrap" | "update"
    status: inv.SpaceStatus
    pages: list[inv.PageInfo]
    related: list[tuple[inv.PageInfo, str]]


TOOLS = [
    {"name": "list_pages", "description": "Show the documentation space's page tree with page titles and file paths.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "doc_inventory", "description": "List every page with its kind (business, tutorial, reference, "
     "technical), title, size, the code it covers and its first paragraph.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_doc_files", "description": "List files in the documentation repo (as currently edited).",
     "input_schema": {"type": "object", "properties": {
         "prefix": {"type": "string", "description": "Optional directory prefix, e.g. 'tutorials/'."}}}},
    {"name": "read_doc_file", "description": "Read a documentation file (including your pending edits).",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "search_docs", "description": "Search all documentation files (including your pending edits) for a "
     "regular expression, case-insensitive. Returns matching lines with file and line number.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "write_doc_file", "description": "Create or overwrite a documentation file with its full new content, "
     "starting with front matter (type, covers).",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "delete_doc_file", "description": "Delete a documentation file that is obsolete.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "code_overview", "description": "Overview of the code repository at the PR head: size, languages, "
     "layout, key files, and where business logic, interfaces and infrastructure probably live.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_code_files", "description": "List files in the code repo at the PR head commit. Large folders "
     "are summarized; pass a prefix to list a folder, or depth to group by folder.",
     "input_schema": {"type": "object", "properties": {
         "prefix": {"type": "string", "description": "Folder to list, e.g. 'src/billing'."},
         "depth": {"type": "integer", "description": "Group results by folder to this depth."}}}},
    {"name": "read_code_file", "description": "Read a file from the code repo at the PR head commit. For long "
     "files, pass start_line and end_line (1-based); results then show line numbers.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
         "required": ["path"]}},
    {"name": "search_code", "description": "Search the code repo at the PR head for a regular expression "
     "(case-insensitive). Returns 'path:line: text'. Narrow with a folder prefix or a file glob like '*.py'.",
     "input_schema": {"type": "object", "properties": {
         "pattern": {"type": "string"}, "prefix": {"type": "string"}, "glob": {"type": "string"}},
         "required": ["pattern"]}},
    {"name": "finish", "description": "Finish the run. Call exactly once, at the end.",
     "input_schema": {"type": "object", "properties": {
         "summary": {"type": "string", "description": "Reviewer-facing summary in Markdown, grouped by kind of "
                     "documentation."},
         "changes_needed": {"type": "boolean"}}, "required": ["summary", "changes_needed"]}},
]
TOOL_NAMES = [t["name"] for t in TOOLS]


class DocWorkspace:
    """In-memory overlay of pending edits on top of the doc branch."""

    def __init__(self, list_base: Callable[[], list[str]], read_base: Callable[[str], str | None]):
        self._list_base, self._read_base = list_base, read_base
        self.changes: dict[str, str | None] = {}

    def list(self, prefix: str = "") -> list[str]:
        files = set(self._list_base())
        for p, c in self.changes.items():
            (files.add if c is not None else files.discard)(p)
        prefix = prefix.lstrip("/")
        return sorted(f for f in files if f.startswith(prefix))

    def outline(self) -> str:
        files = self.list()
        return outline(files, {f: self.read(f) for f in files if is_page(f)})

    def pages(self) -> list[inv.PageInfo]:
        return inv.inventory(self.list(), self.read)

    def read(self, path: str) -> str | None:
        path = validate_path(path)
        if path in self.changes:
            return self.changes[path]
        return self._read_base(path)

    def write(self, path: str, content: str) -> None:
        path = validate_path(path)
        if self._read_base(path) == content:
            self.changes.pop(path, None)
        else:
            self.changes[path] = content

    def delete(self, path: str) -> bool:
        path = validate_path(path)
        if self.read(path) is None:
            return False
        if self._read_base(path) is None:
            self.changes.pop(path, None)
        else:
            self.changes[path] = None
        return True

    def search(self, pattern: str) -> str:
        try:
            rx = re.compile(pattern, re.I)
        except re.error:
            rx = re.compile(re.escape(pattern), re.I)
        hits = []
        for path in self.list():
            if not is_page(path):
                continue
            for no, line in enumerate((self.read(path) or "").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{path}:{no}: {line.strip()[:200]}")
        if not hits:
            return "(no matches)"
        return "\n".join(hits[:150]) + (f"\n[... {len(hits) - 150} more matches]" if len(hits) > 150 else "")


def _truncate(text: str, limit: int, label: str) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... {label} truncated, {len(text) - limit} more characters ...]"


def plan_run(ctx: CodeContext, ws: DocWorkspace) -> RunPlan:
    pages = ws.pages()
    st = inv.status(pages)
    changed = [f["filename"] for f in ctx.files]
    return RunPlan("bootstrap" if st.empty else "update", st, pages, inv.related(pages, changed, ws.read))


def build_task_message(ctx: CodeContext, plan: RunPlan, doc_outline: str, code_overview: str = "",
                       instructions: str = "", feedback: list[str] | None = None,
                       existing_doc_diff: str = "") -> str:
    files = "\n".join(f"- {f['status']}: {f['filename']} (+{f.get('additions', 0)}/-{f.get('deletions', 0)})"
                      for f in ctx.files[:300])
    if plan.mode == "bootstrap":
        mode = ("## Mode: bootstrap\nThe documentation space is empty. Study the codebase and write the "
                "documentation set (business, tutorials, reference, technical) as described in your instructions. "
                "Include what this pull request changes.")
    else:
        mode = "## Mode: update\n" + inv.format_status(plan.status)
        if plan.status.missing:
            mode += ("\nCreate the missing kinds of documentation (based on the whole codebase), as well as "
                     "updating the pages this change affects.")
    parts = [
        f"# Code pull request {ctx.repo_full_name}#{ctx.pr_number}: {ctx.title}",
        f"Author: {ctx.author} · base: {ctx.base_ref} · head: {ctx.head_sha[:12]}",
        "## Description\n" + (ctx.body.strip() or "_No description._"),
        "## Changed files\n" + (files or "_none_"),
        ("## Diff of new commits since your last run\n" if ctx.incremental else "## Diff\n")
        + "```diff\n" + _truncate(ctx.diff, MAX_DIFF_CHARS, "diff") + "\n```",
        mode,
    ]
    if code_overview:
        parts.append("## Code repository overview\n" + _truncate(code_overview, 12_000, "overview"))
    if plan.mode == "update":
        if plan.related:
            parts.append("## Pages related to the changed code\n" + "\n".join(
                f"- {p.path} ({p.type or 'unclassified'}): {why}" for p, why in plan.related))
        else:
            parts.append("## Pages related to the changed code\nNone found from `covers` front matter or file "
                         "mentions. Search the docs for the names this change touches before concluding.")
        parts.append("## Documentation inventory\n" + _truncate(inv.format_inventory(plan.pages), 40_000, "inventory"))
    parts.append("## Documentation space (page tree)\n" + _truncate(doc_outline, 20_000, "page tree"))
    if existing_doc_diff.strip():
        parts.append("## Documentation changes already on this doc PR\n```diff\n"
                     + _truncate(existing_doc_diff, 40_000, "doc diff") + "\n```")
    if instructions.strip():
        parts.append("## Repository-specific instructions\n" + instructions.strip())
    if feedback:
        parts.append("## Reviewer feedback to address\n" + "\n\n".join(f"- {f}" for f in feedback))
    parts.append("Update the documentation accordingly, then call `finish`.")
    return "\n\n".join(parts)


def compact(messages: list[dict], budget: int = CONTEXT_BUDGET_CHARS, keep_last: int = 6) -> int:
    """Drop the oldest large tool outputs and page bodies once the conversation outgrows `budget`.

    The first message (the task) and the last `keep_last` messages are never touched. Returns how
    many items were dropped.
    """
    def size(m: dict) -> int:
        c = m["content"]
        return len(c) if isinstance(c, str) else len(json.dumps(c, ensure_ascii=False))

    total = sum(size(m) for m in messages)
    dropped = 0
    for m in messages[1:max(1, len(messages) - keep_last)]:
        if total <= budget:
            break
        if not isinstance(m["content"], list):
            continue
        for b in m["content"]:
            before = len(json.dumps(b, ensure_ascii=False))
            if b.get("type") == "tool_result" and isinstance(b.get("content"), str) and len(b["content"]) > 800:
                b["content"] = "[Earlier output removed to save space. Run the tool again if you still need it.]"
            elif b.get("type") == "tool_use" and isinstance((b.get("input") or {}).get("content"), str) \
                    and len(b["input"]["content"]) > 800:
                b["input"] = {**b["input"], "content": f"<{len(b['input']['content'])} characters, written>"}
            else:
                continue
            total -= before - len(json.dumps(b, ensure_ascii=False))
            dropped += 1
    return dropped


class DocAgent:
    def __init__(self, messages: MessagesAPI, model: str, max_turns: int = 40, max_tokens: int = 8192,
                 bootstrap_max_turns: int = 150):
        self.messages, self.model, self.max_turns, self.max_tokens = messages, model, max_turns, max_tokens
        self.bootstrap_max_turns = bootstrap_max_turns

    async def run(self, ctx: CodeContext, workspace: DocWorkspace, code: Codebase,
                  instructions: str = "", feedback: list[str] | None = None,
                  existing_doc_diff: str = "") -> AgentResult:
        plan = plan_run(ctx, workspace)
        overview = await _safe_overview(code)
        transcript: list[str] = [f"📋 Mode: {plan.mode} · {inv.format_status(plan.status).splitlines()[0]}"]
        messages: list[dict] = [{"role": "user", "content": build_task_message(
            ctx, plan, workspace.outline(), overview, instructions, feedback or [], existing_doc_diff)}]
        summary, finished = "", False
        max_turns = self.bootstrap_max_turns if plan.mode == "bootstrap" or plan.status.missing else self.max_turns

        for turn in range(1, max_turns + 1):
            if compact(messages):
                transcript.append("🧹 Older tool output dropped from the conversation to save space.")
            resp = await self.messages.create(model=self.model, max_tokens=self.max_tokens,
                                              system=SYSTEM_PROMPT, tools=TOOLS, messages=messages)
            content = [_block_to_dict(b) for b in resp.content]
            messages.append({"role": "assistant", "content": content})
            for b in content:
                if b["type"] == "text" and b["text"].strip():
                    transcript.append(f"💬 {b['text'].strip()}")

            tool_uses = [b for b in content if b["type"] == "tool_use"]
            if not tool_uses:
                if resp.stop_reason == "end_turn":
                    messages.append({"role": "user", "content": "Call `finish` with a summary to complete the run."})
                    continue
                break

            results = []
            for tu in tool_uses:
                name, args = tu["name"], tu.get("input") or {}
                try:
                    if name == "finish":
                        summary = str(args.get("summary", "")).strip()
                        finished = True
                        out = "ok"
                    else:
                        out = await call_tool(name, args, workspace, code)
                    is_error = False
                except Exception as e:  # tool errors go back to the model, not up the stack
                    out, is_error = f"Error: {e}", True
                transcript.append(f"🔧 {name}({_short_args(args)}){' → error' if is_error else ''}")
                results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": out,
                                **({"is_error": True} if is_error else {})})
            messages.append({"role": "user", "content": results})
            if finished:
                return AgentResult(dict(workspace.changes), summary, True, turn, transcript, plan.mode)

        transcript.append("⚠️ Stopped before calling finish (turn limit reached).")
        return AgentResult(dict(workspace.changes), summary or "Agent stopped before finishing.", False,
                           max_turns, transcript, plan.mode)


async def _safe_overview(code: Codebase | None) -> str:
    if code is None:
        return ""
    try:
        return await code.overview()
    except Exception as e:  # the run can still work from the diff
        log.warning("code overview failed: %s", e)
        return ""


def _int(v: Any) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


async def call_tool(name: str, args: dict, ws: DocWorkspace, code: Codebase | None) -> str:
    """Run one of the agent's tools (shared by every provider). Raises on bad input."""
    if name == "list_pages":
        return ws.outline()
    if name == "doc_inventory":
        pages = ws.pages()
        return inv.format_status(inv.status(pages)) + "\n\n" + inv.format_inventory(pages)
    if name == "list_doc_files":
        return "\n".join(ws.list(args.get("prefix", ""))) or "(no files)"
    if name == "read_doc_file":
        content = ws.read(args["path"])
        return "(file does not exist)" if content is None else _truncate(content, MAX_FILE_CHARS, "file")
    if name == "search_docs":
        return ws.search(str(args["pattern"]))
    if name == "write_doc_file":
        path, content = args["path"], args["content"]
        ws.write(path, content)
        note = ""
        if is_page(path):
            meta, _ = parse_front_matter(content)
            if meta.get("type", "").lower() not in inv.ALIASES:
                note = (" Note: this page has no `type:` front matter (business, tutorial, reference or technical); "
                        "add it, with `covers:`, so the page can be matched to future code changes.")
        return f"Wrote {path} ({len(content)} chars).{note}"
    if name == "delete_doc_file":
        return f"Deleted {args['path']}." if ws.delete(args["path"]) else "(file does not exist)"
    if code is None and name in ("code_overview", "list_code_files", "read_code_file", "search_code"):
        return "(the code repository is not available in this run)"
    if name == "code_overview":
        return await code.overview()
    if name == "list_code_files":
        return await code.list(str(args.get("prefix") or ""), _int(args.get("depth")))
    if name == "read_code_file":
        content = await code.read(validate_path(args["path"]), _int(args.get("start_line")), _int(args.get("end_line")))
        return "(file does not exist)" if content is None else content
    if name == "search_code":
        return await code.search(str(args["pattern"]), str(args.get("prefix") or ""), str(args.get("glob") or ""))
    raise ValueError(f"Unknown tool {name}")


def _block_to_dict(block: Any) -> dict:
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True)
    d = {"type": block.type}
    for attr in ("text", "id", "name", "input"):
        if hasattr(block, attr):
            d[attr] = getattr(block, attr)
    return d


def _short_args(args: dict) -> str:
    shown = {k: (v if not isinstance(v, str) or len(v) < 60 else f"<{len(v)} chars>") for k, v in args.items()}
    return json.dumps(shown, ensure_ascii=False)[:160]
