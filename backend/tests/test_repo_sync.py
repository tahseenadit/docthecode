"""Two DocTheCode instances with separate disks sharing one database (MemoryBundleStore)."""
import pytest

from app.gitstore import Author, GitStore, RefMoved
from app.repo_sync import MemoryBundleStore, SyncedGitStore

A = Author("Ada", "ada@example.com")


@pytest.fixture
def pair(tmp_path):
    db = MemoryBundleStore()
    return db, SyncedGitStore(tmp_path / "laptop", db), SyncedGitStore(tmp_path / "server", db)


def test_repo_created_on_one_instance_is_visible_on_another(pair):
    db, a, b = pair
    a.init_repo("docs", "main", A, {"README.md": "hi\n"})
    assert db.version("docs") == 1
    assert b.read_file("docs", "main", "README.md") == "hi\n"


def test_writes_propagate_both_ways(pair):
    db, a, b = pair
    a.init_repo("docs", "main", A, {"README.md": "v1\n"})
    b.create_branch("docs", "pr/1", "main")
    b.commit("docs", "pr/1", {"guide.md": "new\n"}, "add guide", A)
    assert a.read_file("docs", "pr/1", "guide.md") == "new\n"
    a.merge("docs", "main", "pr/1", "merge", A)
    assert sorted(b.list_files("docs", "main")) == ["README.md", "guide.md"]
    # every outer write bumps the version exactly once (nested calls do not re-publish)
    assert db.version("docs") == 4


def test_reads_do_not_publish(pair):
    db, a, b = pair
    a.init_repo("docs", "main", A)
    for _ in range(3):
        b.list_files("docs", "main")
        b.diff("docs", "main", "main")
    assert db.version("docs") == 1


def test_lost_race_is_rolled_back(pair):
    db, a, b = pair
    a.init_repo("docs", "main", A, {"x.md": "0"})
    b.read_file("docs", "main", "x.md")  # b's cache is at v1
    a.commit("docs", "main", {"x.md": "from a"}, "a", A)  # v2

    # Simulate b having missed the version check (a write landed in between).
    orig = b._ensure_fresh
    b._ensure_fresh = lambda repo_id: None
    with pytest.raises(RefMoved):
        b.commit("docs", "main", {"x.md": "from b"}, "b", A)
    b._ensure_fresh = orig
    assert b.read_file("docs", "main", "x.md") == "from a"
    assert db.version("docs") == 2


def test_ephemeral_disk_is_rebuilt(pair, tmp_path):
    db, a, _ = pair
    a.init_repo("docs", "main", A, {"x.md": "keep me"})
    fresh = SyncedGitStore(tmp_path / "new-container", db)
    assert fresh.read_file("docs", "main", "x.md") == "keep me"


def test_legacy_local_repo_is_uploaded(tmp_path):
    GitStore(tmp_path / "disk").init_repo("old", "main", A, {"a.md": "legacy"})
    db = MemoryBundleStore()
    s = SyncedGitStore(tmp_path / "disk", db)
    assert s.read_file("old", "main", "a.md") == "legacy"
    assert db.version("old") == 1


def test_delete(pair):
    db, a, b = pair
    a.init_repo("docs", "main", A)
    b.list_files("docs", "main")
    a.delete_repo("docs")
    assert db.version("docs") is None
    assert not b.exists("docs")
