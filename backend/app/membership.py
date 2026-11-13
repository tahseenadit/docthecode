"""Pure rules for inheriting GitHub repo members (no I/O, easy to test)."""
from __future__ import annotations

from dataclasses import dataclass

_GH_TO_ROLE = {"admin": "admin", "maintain": "write", "write": "write", "triage": "read", "read": "read"}
_PERMISSION_ORDER = ("admin", "maintain", "push", "triage", "pull")
_PERMISSION_NAME = {"push": "write", "pull": "read"}


def permission_from(perms: dict | None) -> str:
    perms = perms or {}
    for p in _PERMISSION_ORDER:
        if perms.get(p):
            return _PERMISSION_NAME.get(p, p)
    return "read"


def role_for(permission: str | None) -> str | None:
    return _GH_TO_ROLE.get(permission or "")


@dataclass(frozen=True)
class Collaborator:
    github_id: int
    login: str
    avatar_url: str | None
    permission: str


def collaborators_from_api(items: list[dict]) -> list[Collaborator]:
    out = {}
    for c in items:
        if c.get("type") == "Bot":
            continue
        out[c["id"]] = Collaborator(c["id"], c["login"], c.get("avatar_url"),
                                    c.get("role_name") if c.get("role_name") in _GH_TO_ROLE
                                    else permission_from(c.get("permissions")))
    return list(out.values())


def plan_member_sync(existing: dict[int, tuple[str, str]], fetched: list[Collaborator]):
    """existing: github_id -> (login, permission). Returns (added, changed, removed_ids)."""
    seen = {c.github_id for c in fetched}
    added = [c for c in fetched if c.github_id not in existing]
    changed = [c for c in fetched if c.github_id in existing and existing[c.github_id] != (c.login, c.permission)]
    removed = [gid for gid in existing if gid not in seen]
    return added, changed, removed


