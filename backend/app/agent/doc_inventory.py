"""What the documentation space already contains, in terms the agent can act on.

Every page belongs to one of four kinds of documentation:

  business   what the product does and the rules it follows, in the domain's own language
  tutorial   step-by-step lessons and how-to guides for a reader's real tasks
  reference  exact descriptions of interfaces: APIs, CLI, configuration, events, data
  technical  how the system is built and run: architecture, components, data flow, operations

The agent records a page's kind and the code it describes in front matter:

  ---
  type: reference
  covers: src/api/invoices/*, src/billing/tax.py
  ---

`type` makes the space's coverage measurable; `covers` (comma-separated paths or globs) lets
DocTheCode find the pages a pull request affects without asking the model. Pages without front
matter are classified from their location (e.g. `tutorials/…`, `reference/…`).

Pure functions only.
"""
from __future__ import annotations

import fnmatch
import posixpath
import re
from dataclasses import dataclass, field

from ..pages import INDEX_NAMES, is_page, page_title, parse_front_matter

DOC_TYPES = ("business", "tutorial", "reference", "technical")
LABELS = {"business": "Business", "tutorial": "Tutorials", "reference": "Reference", "technical": "Technical"}
ALIASES = {
    "business": "business", "product": "business", "domain": "business", "concept": "business",
    "concepts": "business", "explanation": "business",
    "tutorial": "tutorial", "tutorials": "tutorial", "guide": "tutorial", "guides": "tutorial",
    "how-to": "tutorial", "howto": "tutorial",
    "reference": "reference", "api": "reference",
    "technical": "technical", "tech": "technical", "architecture": "technical", "engineering": "technical",
    "operations": "technical",
}
PATH_WORDS: dict[str, tuple[str, ...]] = {
    "tutorial": ("tutorial", "tutorials", "getting-started", "get-started", "quickstart", "quick-start", "guide",
                 "guides", "how-to", "howto", "how-tos", "walkthrough", "walkthroughs", "learn", "onboarding"),
    "reference": ("reference", "references", "api", "apis", "api-reference", "cli", "commands", "config",
                  "configuration", "settings", "endpoints", "schema", "schemas", "events", "webhooks", "sdk",
                  "environment-variables", "errors", "error-codes"),
    "business": ("business", "product", "products", "domain", "glossary", "rules", "business-rules", "processes",
                 "process", "workflows", "features", "use-cases", "policies", "pricing", "billing", "roles",
                 "permissions", "faq", "concepts", "overview"),
    "technical": ("technical", "architecture", "design", "internals", "development", "developers", "developer",
                  "contributing", "operations", "ops", "deployment", "deploying", "infrastructure", "runbooks",
                  "runbook", "adr", "adrs", "decisions", "security", "data-model", "monitoring", "troubleshooting"),
}


@dataclass
class PageInfo:
    path: str
    title: str
    type: str | None          # one of DOC_TYPES, or None when it can't be told
    type_source: str          # "front matter" | "location" | ""
    covers: list[str] = field(default_factory=list)
    summary: str = ""
    words: int = 0


def doc_type(path: str, content: str | None) -> tuple[str | None, str]:
    meta, _ = parse_front_matter(content or "")
    declared = ALIASES.get(meta.get("type", "").strip().lower())
    if declared:
        return declared, "front matter"
    stem = re.sub(r"\.(md|markdown|mdx)$", "", path.lower())
    *folders, name = stem.split("/")
    # The section a page sits in says more than its topic: "guides/deploying" is a tutorial.
    for seg in [*reversed(folders), name]:
        for kind in ("tutorial", "reference", "technical", "business"):
            if seg in PATH_WORDS[kind]:
                return kind, "location"
    return None, ""


def covers(content: str | None) -> list[str]:
    meta, _ = parse_front_matter(content or "")
    return [c.strip().strip("`") for c in meta.get("covers", "").split(",") if c.strip()]


def _summary(content: str) -> tuple[str, int]:
    _, body = parse_front_matter(content)
    text = re.sub(r"```.*?```", " ", body, flags=re.S)
    words = len(re.findall(r"\w+", text))
    for para in re.split(r"\n\s*\n", text):
        p = para.strip()
        if p and not p.startswith(("#", "|", "-", "*", ">", "<", "!")):
            p = re.sub(r"\s+", " ", re.sub(r"[`*_\[\]]", "", p))
            return (p[:180] + ("…" if len(p) > 180 else "")), words
    return "", words


def inventory(files: list[str], read) -> list[PageInfo]:
    """One entry per page. `read(path)` returns the file's current content."""
    out = []
    for path in files:
        if not is_page(path):
            continue
        content = read(path) or ""
        kind, source = doc_type(path, content)
        summary, words = _summary(content)
        out.append(PageInfo(path, page_title(path, content), kind, source, covers(content), summary, words))
    return out


@dataclass
class SpaceStatus:
    empty: bool
    counts: dict[str, int]
    unclassified: int
    pages: int

    @property
    def missing(self) -> list[str]:
        return [t for t in DOC_TYPES if not self.counts.get(t)]


def status(pages: list[PageInfo]) -> SpaceStatus:
    counts = {t: sum(1 for p in pages if p.type == t) for t in DOC_TYPES}
    unclassified = sum(1 for p in pages if p.type is None)
    # "Empty" also covers a fresh space that only has a short home page.
    substantive = [p for p in pages if p.words >= 60 or posixpath.basename(p.path) not in INDEX_NAMES]
    empty = not substantive or sum(p.words for p in pages) < 80
    return SpaceStatus(empty, counts, unclassified, len(pages))


def _matches(pattern: str, path: str) -> bool:
    pattern = pattern.strip().removeprefix("./").lstrip("/")
    if not pattern:
        return False
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if any(c in pattern for c in "*?["):
        return fnmatch.fnmatch(path, pattern) or path.startswith(pattern.rstrip("*").rstrip("/") + "/")
    return path == pattern or path.startswith(pattern + "/")


def related(pages: list[PageInfo], changed: list[str], read) -> list[tuple[PageInfo, str]]:
    """Pages that document the changed code: declared in `covers`, or mentioning a changed file."""
    found: list[tuple[PageInfo, str]] = []
    names = {c: posixpath.basename(c) for c in changed}
    for p in pages:
        hits = sorted({c for c in changed for pat in p.covers if _matches(pat, c)})
        if hits:
            found.append((p, "covers " + ", ".join(hits[:5]) + (" …" if len(hits) > 5 else "")))
            continue
        _, text = parse_front_matter(read(p.path) or "")
        mentioned = sorted({c for c in changed if c in text or (len(names[c]) >= 8 and names[c] in text)})
        if mentioned:
            found.append((p, "mentions " + ", ".join(mentioned[:5]) + (" …" if len(mentioned) > 5 else "")))
    return found[:30]


def format_inventory(pages: list[PageInfo], limit: int = 400) -> str:
    if not pages:
        return "(no pages yet)"
    lines = []
    for p in sorted(pages, key=lambda x: x.path)[:limit]:
        kind = p.type or "unclassified"
        extra = f" · covers: {', '.join(p.covers)}" if p.covers else ""
        lines.append(f"- {p.path} · {kind}{' (from location)' if p.type_source == 'location' else ''} · "
                     f"“{p.title}” · {p.words} words{extra}" + (f"\n  {p.summary}" if p.summary else ""))
    if len(pages) > limit:
        lines.append(f"[... {len(pages) - limit} more pages; use list_pages]")
    return "\n".join(lines)


def format_status(st: SpaceStatus) -> str:
    if st.empty:
        return "The documentation space is empty (or has only a placeholder home page)."
    parts = [f"{LABELS[t]}: {st.counts[t]} page(s)" for t in DOC_TYPES]
    line = f"{st.pages} pages. " + " · ".join(parts)
    if st.unclassified:
        line += f" · unclassified: {st.unclassified}"
    if st.missing:
        line += "\nNo pages yet for: " + ", ".join(LABELS[t] for t in st.missing) + "."
    return line
