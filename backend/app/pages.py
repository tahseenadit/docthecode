"""Confluence-style pages on top of a plain git file tree.

A doc repo is organized for readers, not like the code it documents:

  index.md                    the space's home page
  getting-started.md          a page
  guides/                     a section (a page that has child pages)
    index.md                  the section's own page (title, intro, links to children)
    deploying.md              a child page
  images/diagram.png          an attachment

  * A page's title comes from front matter (`title:`), else its first `# heading`,
    else its file name. Front matter `order:` sorts siblings; otherwise by title.
  * Adding a child to a plain page turns it into a section: `foo.md` moves to
    `foo/index.md` and the child goes in `foo/`.

Pure functions only, so the structure rules are easy to test and reuse in the agent.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .gitstore import CopyFrom

Change = "str | CopyFrom | None"

PAGE_EXT = (".md", ".markdown", ".mdx")
INDEX_NAMES = ("index.md", "README.md", "readme.md")
_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)


def is_page(path: str) -> bool:
    return path.lower().endswith(PAGE_EXT)


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Minimal `key: value` front matter. Returns (meta, body)."""
    m = _FM.match(text or "")
    if not m:
        return {}, text or ""
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            k, v = line.split(":", 1)
            meta[k.strip().lower()] = v.strip().strip("'\"")
    return meta, text[m.end():]


def _humanize(name: str) -> str:
    stem = re.sub(r"\.(md|markdown|mdx)$", "", name, flags=re.I)
    stem = re.sub(r"^\d+[-_ ]+", "", stem)  # "01-intro" -> "intro"
    return stem.replace("-", " ").replace("_", " ").strip().capitalize() or name


def page_title(path: str, content: str | None) -> str:
    meta, body = parse_front_matter(content or "")
    if meta.get("title"):
        return meta["title"]
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
        if line.strip():
            break
    parts = path.split("/")
    if parts[-1] in INDEX_NAMES:
        return _humanize(parts[-2]) if len(parts) > 1 else "Home"
    return _humanize(parts[-1])


def page_order(content: str | None) -> float:
    meta, _ = parse_front_matter(content or "")
    try:
        return float(meta.get("order", ""))
    except ValueError:
        return float("inf")


def set_title(content: str, title: str) -> str:
    """Change a page's title where it is defined (front matter, else the first heading)."""
    title = title.strip()
    m = _FM.match(content)
    if m and re.search(r"(?m)^title\s*:", m.group(1)):
        fm = re.sub(r"(?m)^title\s*:.*$", f"title: {title}", m.group(1))
        return f"---\n{fm}\n---\n" + content[m.end():]
    head, body = (content[:m.end()], content[m.end():]) if m else ("", content)
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("# "):
            lines[i] = f"# {title}"
            return head + "\n".join(lines)
        if line.strip():
            break
    return head + f"# {title}\n\n" + body.lstrip("\n")


def slugify(title: str) -> str:
    s = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:60] or "page"


# ------------------------------------------------------------------ tree
@dataclass
class Node:
    kind: str                      # "page" | "section" | "file"
    title: str
    path: str | None               # file to open (a section's index page, if it has one)
    dir: str | None = None         # for sections
    order: float = float("inf")
    children: list["Node"] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "title": self.title, "path": self.path, "dir": self.dir,
                "children": [c.to_dict() for c in self.children]}


def _index_of(files: set[str], d: str) -> str | None:
    for name in INDEX_NAMES:
        p = f"{d}/{name}" if d else name
        if p in files:
            return p
    return None


def build_tree(files: list[str], contents: dict[str, str | None]) -> dict:
    """Return {"home": path|None, "children": [...]} for the whole repo."""
    fileset = set(files)

    def build(d: str) -> list[Node]:
        prefix = f"{d}/" if d else ""
        direct, subdirs = [], set()
        for f in files:
            if not f.startswith(prefix):
                continue
            rest = f[len(prefix):]
            if "/" in rest:
                subdirs.add(rest.split("/", 1)[0])
            else:
                direct.append(f)
        own_index = _index_of(fileset, d)
        nodes: list[Node] = []
        for f in direct:
            if f == own_index:
                continue
            if is_page(f):
                nodes.append(Node("page", page_title(f, contents.get(f)), f, order=page_order(contents.get(f))))
            else:
                nodes.append(Node("file", f.rsplit("/", 1)[-1], f))
        for sd in subdirs:
            full = f"{prefix}{sd}"
            idx = _index_of(fileset, full)
            title = page_title(idx, contents.get(idx)) if idx else _humanize(sd)
            nodes.append(Node("section", title, idx, dir=full, order=page_order(contents.get(idx)) if idx else float("inf"),
                              children=build(full)))
        kind_rank = {"section": 0, "page": 0, "file": 1}  # attachments after pages
        nodes.sort(key=lambda n: (kind_rank[n.kind], n.order, n.title.lower()))
        return nodes

    return {"home": _index_of(fileset, ""), "children": [n.to_dict() for n in build("")]}


def outline(files: list[str], contents: dict[str, str | None]) -> str:
    """Indented text outline for the agent: `- Title (path)`."""
    tree = build_tree(files, contents)
    lines = [f"- Home ({tree['home']})"] if tree["home"] else []

    def walk(nodes: list[dict], depth: int) -> None:
        for n in nodes:
            pad = "  " * depth
            if n["kind"] == "section":
                lines.append(f"{pad}- {n['title']}/ (section {n['dir']}/{', page ' + n['path'] if n['path'] else ''})")
                walk(n["children"], depth + 1)
            elif n["kind"] == "page":
                lines.append(f"{pad}- {n['title']} ({n['path']})")
            else:
                lines.append(f"{pad}- [file] {n['path']}")

    walk(tree["children"], 0)
    return "\n".join(lines) or "(empty)"


# ------------------------------------------------------------------ structural edits
def _unique(path: str, taken: set[str]) -> str:
    if path not in taken:
        return path
    stem, ext = (path[:-3], ".md") if path.endswith(".md") else (path, "")
    i = 2
    while f"{stem}-{i}{ext}" in taken:
        i += 1
    return f"{stem}-{i}{ext}"


def _dir_taken(d: str, files: set[str]) -> bool:
    return any(f.startswith(d + "/") for f in files)


def resolve_parent(parent: str, files: set[str]) -> tuple[str, dict[str, Change]]:
    """Directory that children of `parent` go in, plus changes needed to make it a section.

    `parent` may be "" (top level), a section directory, a section's index page, or a plain page
    (which is then converted into a section: foo.md -> foo/index.md).
    """
    parent = parent.strip("/")
    if not parent:
        return "", {}
    if _dir_taken(parent, files):
        return parent, {}
    name = parent.rsplit("/", 1)[-1]
    if name in INDEX_NAMES:
        return parent.rsplit("/", 1)[0] if "/" in parent else "", {}
    if parent in files and is_page(parent):
        d = re.sub(r"\.(md|markdown|mdx)$", "", parent, flags=re.I)
        return d, {f"{d}/index.md": CopyFrom(parent), parent: None}
    raise ValueError(f"No such page or section: {parent}")


def _apply(files: set[str], changes: dict[str, Change]) -> set[str]:
    return (files | {p for p, c in changes.items() if c is not None}) - {p for p, c in changes.items() if c is None}


def plan_new_page(files: list[str], parent: str, title: str, body: str = "") -> tuple[str, dict[str, Change]]:
    d, changes = resolve_parent(parent, set(files))
    path = _unique(f"{d}/{slugify(title)}.md" if d else f"{slugify(title)}.md", _apply(set(files), changes))
    changes[path] = f"# {title.strip()}\n\n{body.strip()}\n" if body.strip() else f"# {title.strip()}\n\n"
    return path, changes


def plan_move(files: list[str], src: str, new_parent: str) -> tuple[str, dict[str, Change]]:
    """Move a page, or a whole section (given as its directory or its index page), under new_parent.

    Returns the new path (file for a page, directory for a section) and the changes. Files are
    copied by git object, so attachments such as images move byte-for-byte.
    """
    fileset = set(files)
    src = src.strip("/")
    if src.rsplit("/", 1)[-1] in INDEX_NAMES and "/" in src:
        src = src.rsplit("/", 1)[0]
    is_section = _dir_taken(src, fileset)
    if not is_section and src not in fileset:
        raise ValueError(f"No such page: {src}")
    if is_section and (new_parent.strip("/") == src or new_parent.strip("/").startswith(src + "/")):
        raise ValueError("Cannot move a section into itself")
    target_dir, changes = resolve_parent(new_parent, fileset)
    name = src.rsplit("/", 1)[-1]
    dest = f"{target_dir}/{name}" if target_dir else name
    if dest == src:
        return src, {}
    moving = sorted(f for f in fileset if f.startswith(src + "/")) if is_section else [src]
    remaining = _apply(fileset, changes) - set(moving)
    if is_section:
        if _dir_taken(dest, remaining):
            raise ValueError(f"{dest}/ already exists")
    else:
        dest = _unique(dest, remaining)
    for f in moving:
        changes[(dest + f[len(src):]) if is_section else dest] = CopyFrom(f)
        changes.setdefault(f, None)
    return dest, changes


def section_files(files: list[str], path: str) -> list[str]:
    """Everything that deleting `path` removes: a page, or a whole section."""
    path = path.strip("/")
    if path.rsplit("/", 1)[-1] in INDEX_NAMES and "/" in path:
        path = path.rsplit("/", 1)[0]
    inside = [f for f in files if f.startswith(path + "/")]
    return inside or ([path] if path in files else [])
