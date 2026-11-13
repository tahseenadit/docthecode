"""The documentation agent.

Given a code pull request, the agent reads the diff, browses the existing docs (and,
when it needs more context, the code repo at the PR head) and proposes edits to the
doc repo through tools. Edits are staged in memory and returned as one change set,
which the sync service commits to the doc PR branch.

The model client is injected (see providers.py for Anthropic, OpenAI, Gemini and other
OpenAI-compatible APIs), so the loop is provider-neutral and testable without network access.
GitHub Copilot runs its own agent loop with the same tools (copilot_agent.py).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from ..gitstore import validate_path
from ..pages import is_page, outline

log = logging.getLogger(__name__)

MAX_DIFF_CHARS = 120_000
MAX_FILE_CHARS = 60_000

SYSTEM_PROMPT = """You are DocTheCode's documentation agent. A developer opened or updated a pull request \
in a code repository. Your job is to keep the linked documentation space accurate.

The documentation space is a wiki for readers, like a Confluence space. It is NOT a mirror of the \
code repository: never recreate source folders or name pages after source files. Organize by what \
readers need:
- `index.md` is the space's home page. A folder is a section; its `index.md` is the section page and \
should briefly introduce and link its child pages.
- Typical top-level sections: Getting started, Guides (task-oriented how-tos), Concepts (how things \
work), Reference (APIs, configuration, CLI), Operations (deploy, monitor, troubleshoot), Changelog. \
Follow the space's existing structure if it already has one.
- Page titles come from the first `# Heading` (or `title:` front matter). File names are lowercase \
slugs of the title, e.g. `guides/rotating-api-keys.md`. An `order: <number>` front matter line \
sorts a page among its siblings.

How to work:
1. Read the code change carefully. Decide what a user, operator or developer of this project needs \
to know: new or changed APIs, configuration, CLI flags, behaviour, migrations, removed features.
2. Look at the page tree (list_pages) and read the relevant pages before writing. Prefer updating \
the page that already covers a topic over creating a new one. Match the existing tone and formatting.
3. Use read_code_file / list_code_files only when the diff alone is not enough context.
4. Make focused, accurate edits with write_doc_file (always the full new file content) or \
delete_doc_file. When you add a page, link it from its section page. Never invent behaviour that \
is not supported by the code.
5. Internal refactors, tests and CI changes usually need no doc changes. If nothing needs updating, \
say so.
6. Always finish by calling `finish` with a short reviewer-facing summary of what you changed and why.

Reviewer feedback on earlier drafts, if any, takes priority: address every point."""


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


TOOLS = [
    {"name": "list_pages", "description": "Show the documentation space's page tree with page titles and file paths.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_doc_files", "description": "List files in the documentation repo (as currently edited).",
     "input_schema": {"type": "object", "properties": {
         "prefix": {"type": "string", "description": "Optional directory prefix, e.g. 'guides/'."}}}},
    {"name": "read_doc_file", "description": "Read a documentation file (including your pending edits).",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_doc_file", "description": "Create or overwrite a documentation file with full new content.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "delete_doc_file", "description": "Delete a documentation file that is obsolete.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "list_code_files", "description": "List files in the code repo at the PR head commit.",
     "input_schema": {"type": "object", "properties": {"prefix": {"type": "string"}}}},
    {"name": "read_code_file", "description": "Read a source file from the code repo at the PR head commit.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "finish", "description": "Finish the run. Call exactly once, at the end.",
     "input_schema": {"type": "object", "properties": {
         "summary": {"type": "string", "description": "Reviewer-facing summary in Markdown."},
         "changes_needed": {"type": "boolean"}}, "required": ["summary", "changes_needed"]}},
]


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


def _truncate(text: str, limit: int, label: str) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... {label} truncated, {len(text) - limit} more characters ...]"


def build_task_message(ctx: CodeContext, doc_outline: str, instructions: str,
                       feedback: list[str], existing_doc_diff: str) -> str:
    files = "\n".join(f"- {f['status']}: {f['filename']} (+{f.get('additions', 0)}/-{f.get('deletions', 0)})"
                      for f in ctx.files[:300])
    parts = [
        f"# Code pull request {ctx.repo_full_name}#{ctx.pr_number}: {ctx.title}",
        f"Author: {ctx.author} · base: {ctx.base_ref} · head: {ctx.head_sha[:12]}",
        "## Description\n" + (ctx.body.strip() or "_No description._"),
        "## Changed files\n" + (files or "_none_"),
        ("## Diff of new commits since your last run\n" if ctx.incremental else "## Diff\n")
        + "```diff\n" + _truncate(ctx.diff, MAX_DIFF_CHARS, "diff") + "\n```",
        "## Documentation space (page tree)\n" + _truncate(doc_outline, 30_000, "page tree"),
    ]
    if existing_doc_diff.strip():
        parts.append("## Documentation changes already on this doc PR\n```diff\n"
                     + _truncate(existing_doc_diff, 40_000, "doc diff") + "\n```")
    if instructions.strip():
        parts.append("## Repository-specific instructions\n" + instructions.strip())
    if feedback:
        parts.append("## Reviewer feedback to address\n" + "\n\n".join(f"- {f}" for f in feedback))
    parts.append("Update the documentation accordingly, then call `finish`.")
    return "\n\n".join(parts)


class DocAgent:
    def __init__(self, messages: MessagesAPI, model: str, max_turns: int = 40, max_tokens: int = 8192):
        self.messages, self.model, self.max_turns, self.max_tokens = messages, model, max_turns, max_tokens

    async def run(self, ctx: CodeContext, workspace: DocWorkspace,
                  read_code: Callable[[str], Awaitable[str | None]],
                  list_code: Callable[[], Awaitable[list[str]]],
                  instructions: str = "", feedback: list[str] | None = None,
                  existing_doc_diff: str = "") -> AgentResult:
        transcript: list[str] = []
        messages: list[dict] = [{"role": "user", "content": build_task_message(
            ctx, workspace.outline(), instructions, feedback or [], existing_doc_diff)}]
        summary, finished = "", False

        for turn in range(1, self.max_turns + 1):
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
                        out = await self._call_tool(name, args, workspace, read_code, list_code)
                    is_error = False
                except Exception as e:  # tool errors go back to the model, not up the stack
                    out, is_error = f"Error: {e}", True
                transcript.append(f"🔧 {name}({_short_args(args)}){' → error' if is_error else ''}")
                results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": out,
                                **({"is_error": True} if is_error else {})})
            messages.append({"role": "user", "content": results})
            if finished:
                return AgentResult(dict(workspace.changes), summary, True, turn, transcript)

        transcript.append("⚠️ Stopped before calling finish (turn limit reached).")
        return AgentResult(dict(workspace.changes), summary or "Agent stopped before finishing.", False,
                           self.max_turns, transcript)

    async def _call_tool(self, name, args, ws, read_code, list_code) -> str:
        return await call_tool(name, args, ws, read_code, list_code)


async def call_tool(name: str, args: dict, ws: DocWorkspace,
                    read_code: Callable[[str], Awaitable[str | None]],
                    list_code: Callable[[], Awaitable[list[str]]]) -> str:
    """Run one of the agent's tools (shared by every provider). Raises on bad input."""
    if name == "list_pages":
        return ws.outline()
    if name == "list_doc_files":
        return "\n".join(ws.list(args.get("prefix", ""))) or "(no files)"
    if name == "read_doc_file":
        content = ws.read(args["path"])
        return "(file does not exist)" if content is None else _truncate(content, MAX_FILE_CHARS, "file")
    if name == "write_doc_file":
        ws.write(args["path"], args["content"])
        return f"Wrote {args['path']} ({len(args['content'])} chars)."
    if name == "delete_doc_file":
        return f"Deleted {args['path']}." if ws.delete(args["path"]) else "(file does not exist)"
    if name == "list_code_files":
        prefix = args.get("prefix", "").lstrip("/")
        files = [f for f in await list_code() if f.startswith(prefix)]
        return "\n".join(files[:2000]) or "(no files)"
    if name == "read_code_file":
        content = await read_code(validate_path(args["path"]))
        return "(file does not exist)" if content is None else _truncate(content, MAX_FILE_CHARS, "file")
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
