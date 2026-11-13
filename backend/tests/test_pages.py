import pytest

from app.gitstore import Author, CopyFrom, GitStore
from app.pages import (build_tree, outline, page_title, plan_move, plan_new_page, section_files, set_title, slugify)

A = Author("Ada", "ada@example.com")

FILES = ["index.md", "getting-started.md", "guides/index.md", "guides/deploy.md", "guides/zz.md",
         "guides/images/arch.png", "reference/cli.md"]
CONTENT = {
    "index.md": "# Payments docs\n",
    "getting-started.md": "---\ntitle: Getting started\norder: 1\n---\nBody",
    "guides/index.md": "# Guides\n",
    "guides/deploy.md": "# Deploying\n",
    "guides/zz.md": "---\norder: 0\n---\n# Aardvark last alphabetically but first by order\n",
    "reference/cli.md": "no heading here",
}


def test_titles():
    assert page_title("getting-started.md", CONTENT["getting-started.md"]) == "Getting started"
    assert page_title("guides/deploy.md", "# Deploying\n") == "Deploying"
    assert page_title("reference/cli.md", "text") == "Cli"
    assert page_title("01-intro.md", "") == "Intro"
    assert page_title("guides/index.md", "") == "Guides"
    assert page_title("index.md", "") == "Home"


def test_tree_structure_and_order():
    tree = build_tree(FILES, CONTENT)
    assert tree["home"] == "index.md"
    top = [(n["kind"], n["title"]) for n in tree["children"]]
    # getting-started has order 1, others sort by title
    assert top == [("page", "Getting started"), ("section", "Guides"), ("section", "Reference")]
    guides = tree["children"][1]
    assert guides["path"] == "guides/index.md" and guides["dir"] == "guides"
    kids = [(n["kind"], n["title"]) for n in guides["children"]]
    assert kids == [("page", "Aardvark last alphabetically but first by order"), ("page", "Deploying"),
                    ("section", "Images")]
    assert guides["children"][2]["children"][0] == {"kind": "file", "title": "arch.png", "path": "guides/images/arch.png",
                                                    "dir": None, "children": []}
    reference = tree["children"][2]
    assert reference["path"] is None  # section without its own page
    assert "- Deploying (guides/deploy.md)" in outline(FILES, CONTENT)


def test_set_title():
    assert set_title("# Old\n\nbody", "New") == "# New\n\nbody"
    assert set_title("---\ntitle: Old\norder: 2\n---\nbody", "New") == "---\ntitle: New\norder: 2\n---\nbody"
    assert set_title("body only", "New") == "# New\n\nbody only"
    assert set_title("---\norder: 2\n---\n# Old\n", "New") == "---\norder: 2\n---\n# New\n"


def test_slugify():
    assert slugify("Deploying to Kubernetes (EKS)!") == "deploying-to-kubernetes-eks"
    assert slugify("Ünïcödé") == "unicode"
    assert slugify("???") == "page"


def test_new_page_top_level_and_in_section():
    path, ch = plan_new_page(FILES, "", "Getting Started")
    assert path == "getting-started-2.md" and ch == {path: "# Getting Started\n\n"}
    path, ch = plan_new_page(FILES, "guides", "Rollbacks")
    assert path == "guides/rollbacks.md"
    path, _ = plan_new_page(FILES, "guides/index.md", "Rollbacks")
    assert path == "guides/rollbacks.md"


def test_child_of_plain_page_converts_it_to_section():
    path, ch = plan_new_page(FILES, "getting-started.md", "Install")
    assert path == "getting-started/install.md"
    assert ch["getting-started/index.md"] == CopyFrom("getting-started.md")
    assert ch["getting-started.md"] is None


def test_move_page_and_section():
    dest, ch = plan_move(FILES, "reference/cli.md", "guides")
    assert dest == "guides/cli.md" and ch == {"guides/cli.md": CopyFrom("reference/cli.md"), "reference/cli.md": None}

    dest, ch = plan_move(FILES, "guides/index.md", "reference")
    assert dest == "reference/guides"
    assert ch["reference/guides/images/arch.png"] == CopyFrom("guides/images/arch.png")
    assert ch["guides/deploy.md"] is None

    with pytest.raises(ValueError):
        plan_move(FILES, "guides", "guides/images")
    assert plan_move(FILES, "guides/deploy.md", "guides") == ("guides/deploy.md", {})


def test_section_files():
    assert sorted(section_files(FILES, "guides/index.md")) == ["guides/deploy.md", "guides/images/arch.png",
                                                               "guides/index.md", "guides/zz.md"]
    assert section_files(FILES, "reference/cli.md") == ["reference/cli.md"]


def test_moves_apply_byte_for_byte_in_git(tmp_path):
    store = GitStore(tmp_path)
    png = "\x89PNG binary-ish \x00\x01"
    store.init_repo("d", "main", A, {"guides/index.md": "# Guides\n", "guides/a.png": png, "ref.md": "# Ref\n"})
    files = store.list_files("d", "main")
    _, ch = plan_move(files, "guides", "ref.md")  # also converts ref.md into a section
    store.commit("d", "main", ch, "move", A)
    assert sorted(store.list_files("d", "main")) == ["ref/guides/a.png", "ref/guides/index.md", "ref/index.md"]
    assert store.read_file("d", "main", "ref/guides/a.png") == png
    got = store.read_many("d", "main", ["ref/index.md", "missing.md", "ref/guides/index.md"])
    assert got == {"ref/index.md": "# Ref\n", "missing.md": None, "ref/guides/index.md": "# Guides\n"}
