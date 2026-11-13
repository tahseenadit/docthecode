from app.membership import Collaborator, collaborators_from_api, permission_from, plan_member_sync, role_for

API = [
    {"id": 1, "login": "ada", "type": "User", "role_name": "admin", "permissions": {"admin": True, "push": True, "pull": True}},
    {"id": 2, "login": "bob", "type": "User", "role_name": "maintain", "permissions": {"maintain": True}},
    {"id": 3, "login": "cy", "type": "User", "permissions": {"push": True, "pull": True}},          # no role_name
    {"id": 4, "login": "dee", "type": "User", "role_name": "custom-auditor", "permissions": {"pull": True}},  # custom role
    {"id": 5, "login": "ci-bot", "type": "Bot", "role_name": "write"},
    {"id": 3, "login": "cy", "type": "User", "role_name": "write"},                                   # duplicate page entry
]


def test_collaborators_from_api():
    got = {c.login: c.permission for c in collaborators_from_api(API)}
    assert got == {"ada": "admin", "bob": "maintain", "cy": "write", "dee": "read"}


def test_github_permissions_map_to_doc_roles():
    assert [role_for(p) for p in ("admin", "maintain", "write", "triage", "read", None)] == \
        ["admin", "write", "write", "read", "read", None]
    assert permission_from({"triage": True, "pull": True}) == "triage"
    assert permission_from({}) == "read"


def test_plan_member_sync():
    existing = {1: ("ada", "admin"), 2: ("bob", "write"), 9: ("gone", "read")}
    fetched = [Collaborator(1, "ada", None, "admin"), Collaborator(2, "bob", None, "maintain"),
               Collaborator(3, "new", None, "read")]
    added, changed, removed = plan_member_sync(existing, fetched)
    assert [c.login for c in added] == ["new"]
    assert [c.login for c in changed] == ["bob"]
    assert removed == [9]
