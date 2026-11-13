import pytest

from app.gitstore import Author, GitStore, MergeConflict, RefMoved

A = Author("Ada", "ada@example.com")


@pytest.fixture
def store(tmp_path):
    s = GitStore(tmp_path)
    s.init_repo("docs", "main", A, {"README.md": "# Docs\n", "guide/intro.md": "Hello\n"})
    return s


def test_init_and_read(store):
    assert store.list_files("docs", "main") == ["README.md", "guide/intro.md"]
    assert store.read_file("docs", "main", "guide/intro.md") == "Hello\n"
    assert store.read_file("docs", "main", "missing.md") is None
    assert store.list_files("docs", "main", prefix="guide") == ["guide/intro.md"]


def test_branch_commit_diff(store):
    store.create_branch("docs", "pr/1", "main")
    store.commit("docs", "pr/1", {"guide/intro.md": "Hello world\n", "api.md": "# API\n", "README.md": None},
                 "Update docs", A)
    changed = {(c.status, c.path) for c in store.changed_files("docs", "main", "pr/1")}
    assert changed == {("M", "guide/intro.md"), ("A", "api.md"), ("D", "README.md")}
    assert "+Hello world" in store.diff("docs", "main", "pr/1")
    assert store.list_files("docs", "main") == ["README.md", "guide/intro.md"]  # main untouched


def test_noop_commit_returns_parent(store):
    head = store.branch_sha("docs", "main")
    assert store.commit("docs", "main", {"README.md": "# Docs\n"}, "noop", A) == head


def test_compare_and_swap(store):
    head = store.branch_sha("docs", "main")
    store.commit("docs", "main", {"x.md": "x"}, "move", A)
    with pytest.raises(RefMoved):
        store.commit("docs", "main", {"y.md": "y"}, "stale", A, expected_sha=head)


def test_merge_clean_after_base_moved(store):
    store.create_branch("docs", "pr/1", "main")
    store.commit("docs", "pr/1", {"api.md": "# API\n"}, "pr change", A)
    store.commit("docs", "main", {"other.md": "other\n"}, "main change", A)
    assert not store.can_fast_forward("docs", "main", "pr/1")
    store.merge("docs", "main", "pr/1", "Merge pr/1", A)
    assert set(store.list_files("docs", "main")) == {"README.md", "guide/intro.md", "api.md", "other.md"}
    assert store.log("docs", "main", limit=1)[0].message == "Merge pr/1"


def test_merge_conflict(store):
    store.create_branch("docs", "pr/1", "main")
    store.commit("docs", "pr/1", {"guide/intro.md": "PR version\n"}, "pr", A)
    store.commit("docs", "main", {"guide/intro.md": "Main version\n"}, "main", A)
    with pytest.raises(MergeConflict) as exc:
        store.merge("docs", "main", "pr/1", "Merge", A)
    assert exc.value.paths == ["guide/intro.md"]


@pytest.mark.parametrize("bad", ["../x", "/.git/config", ".git/HEAD", "a//b", ""])
def test_rejects_bad_paths(store, bad):
    with pytest.raises(ValueError):
        store.commit("docs", "main", {bad: "x"}, "bad", A)
