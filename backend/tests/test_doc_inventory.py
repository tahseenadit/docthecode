from app.agent import doc_inventory as inv

LONG = "Invoices are created when an order ships. " * 20

FILES = {
    "index.md": "# Home\n\n" + LONG,
    "guides/deploying.md": "# Deploying\n\n" + LONG,
    "reference/index.md": "# Reference\n",
    "reference/api.md": "---\ntype: reference\ncovers: src/api/*, src/schemas/\n---\n# API\n\nEndpoints.\n",
    "about/rules.md": "---\ntype: Business\ncovers: src/billing/invoices.py\n---\n# Rules\n\n" + LONG,
    "notes.md": "# Notes\n\nSee src/jobs/nightly_export.py for the export.\n",
    "images/a.png": "binary",
}


def read(p):
    return FILES.get(p)


def pages():
    return inv.inventory(list(FILES), read)


def test_types_from_front_matter_and_location():
    by = {p.path: p for p in pages()}
    assert "images/a.png" not in by
    assert by["reference/api.md"].type == "reference" and by["reference/api.md"].type_source == "front matter"
    assert by["about/rules.md"].type == "business"                       # case-insensitive, any folder
    assert by["guides/deploying.md"].type == "tutorial" and by["guides/deploying.md"].type_source == "location"
    assert by["reference/index.md"].type == "reference"
    assert by["index.md"].type is None and by["notes.md"].type is None
    assert by["reference/api.md"].covers == ["src/api/*", "src/schemas/"]
    assert by["about/rules.md"].summary.startswith("Invoices are created")


def test_status_counts_and_missing_kinds():
    st = inv.status(pages())
    assert not st.empty
    assert st.counts == {"business": 1, "tutorial": 1, "reference": 2, "technical": 0}
    assert st.missing == ["technical"] and st.unclassified == 2
    text = inv.format_status(st)
    assert "No pages yet for: Technical." in text


def test_empty_space_detection():
    assert inv.status([]).empty
    only_home = inv.inventory(["index.md"], lambda p: "# Docs\n\nComing soon.\n")
    assert inv.status(only_home).empty
    assert "empty" in inv.format_status(inv.status(only_home))


def test_related_pages_from_covers_and_mentions():
    changed = ["src/api/invoices.py", "src/schemas/invoice.json", "src/jobs/nightly_export.py", "src/other.py"]
    rel = {p.path: why for p, why in inv.related(pages(), changed, read)}
    assert rel["reference/api.md"] == "covers src/api/invoices.py, src/schemas/invoice.json"
    assert rel["notes.md"] == "mentions src/jobs/nightly_export.py"
    assert "about/rules.md" not in rel
    rel2 = {p.path for p, _ in inv.related(pages(), ["src/billing/invoices.py"], read)}
    assert rel2 == {"about/rules.md"}


def test_cover_patterns():
    m = inv._matches
    assert m("src/api/", "src/api/x.py") and not m("src/api/", "src/apix.py")
    assert m("src/api", "src/api/v1/x.py") and m("cli.py", "cli.py") and not m("cli.py", "tools/cli.py")
    assert m("src/**/*.py", "src/a/b.py") and m("./src/a.py", "src/a.py") and m(".github/workflows/*", ".github/workflows/ci.yml")
    assert not m("", "anything")


def test_inventory_formatting():
    text = inv.format_inventory(pages())
    assert "- reference/api.md · reference · “API” ·" in text and "covers: src/api/*, src/schemas/" in text
    assert "guides/deploying.md · tutorial (from location)" in text
    assert inv.format_inventory([]) == "(no pages yet)"
