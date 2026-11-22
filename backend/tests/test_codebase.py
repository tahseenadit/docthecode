import asyncio
import io
import tarfile

from app.agent.codebase import ApiCodebase, SnapshotCodebase, build_overview


def tarball(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, data in files.items():
            info = tarfile.TarInfo("acme-api-abc123/" + path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def run(coro):
    return asyncio.run(coro)


FILES = {
    "README.md": b"# Acme API\n",
    "src/billing/invoices.py": b"def total(lines):\n    # Orders over 1000 get 5% off\n    return sum(lines)\n",
    "src/api/routes.py": b"@app.post('/invoices')\ndef create(): ...\n",
    "node_modules/left-pad/index.js": b"module.exports = 1\n",
    "logo.png": b"\x89PNG\r\n\x1a\n\0\0",
    "data.bin": b"abc\0def",
    "package-lock.json": b"{}",
    "Dockerfile": b"FROM python:3.13\n",
}


def test_snapshot_keeps_source_and_skips_generated_files():
    code = SnapshotCodebase.from_tarball(tarball(FILES))
    assert set(code.files) == {"README.md", "src/billing/invoices.py", "src/api/routes.py", "Dockerfile"}
    assert code.skipped == 4


def test_overview_points_at_business_reference_and_technical_sources():
    code = SnapshotCodebase.from_tarball(tarball(FILES))
    ov = run(code.overview())
    assert "4 text files" in ov and "Python 2" in ov
    assert "Key files:" in ov and "README.md" in ov and "Dockerfile" in ov
    assert "Business rules and domain logic: src/billing" in ov
    assert "Interfaces to document as reference: src/api" in ov
    assert "4 generated, vendored, binary or very large files left out" in ov


def test_read_whole_file_and_line_ranges():
    code = SnapshotCodebase.from_tarball(tarball(FILES))
    assert run(code.read("src/billing/invoices.py")).startswith("def total")
    part = run(code.read("./src/billing/invoices.py", 2, 2))
    assert part == "2      # Orders over 1000 get 5% off\n[... 1 more lines]"
    assert run(code.read("missing.py")) is None
    assert "only 3 lines" in run(code.read("src/billing/invoices.py", 10, 12))


def test_long_files_are_cut_with_a_hint():
    big = "\n".join(f"line {i} " + "x" * 100 for i in range(2000))
    code = SnapshotCodebase({"big.py": big})
    out = run(code.read("big.py"))
    assert "Read the rest with start_line=" in out and len(out) < 70_000


def test_search_with_prefix_glob_and_bad_regex():
    code = SnapshotCodebase.from_tarball(tarball(FILES))
    out = run(code.search("orders over"))
    assert out.startswith("1 matches in 1 files") and "src/billing/invoices.py:2:" in out
    assert run(code.search("def ", prefix="src/api")).count("src/api/routes.py") == 1
    assert run(code.search("def ", glob="invoices.py")).count("invoices.py") >= 1
    assert "(no matches)" == run(code.search("def ", prefix="docs"))
    assert "routes.py" in run(code.search("app.post('/invoices'"))  # invalid regex falls back to a literal search


def test_list_groups_large_trees():
    files = {f"src/mod{i}/f{j}.py": "x" for i in range(30) for j in range(20)}
    code = SnapshotCodebase(files)
    out = run(code.list())
    assert "600 files" in out and "src/mod1/  (20 files)" in out
    assert run(code.list("src/mod1")).count("\n") == 19


def test_api_fallback_reads_files_and_searches_paths():
    async def paths():
        return ["src/a.py", "node_modules/x.js", "README.md"]

    reads = []

    async def read(p):
        reads.append(p)
        return "hello world" if p == "src/a.py" else None

    code = ApiCodebase(paths, read)
    assert "node_modules" not in run(code.list())
    assert run(code.read("src/a.py")) == "hello world"
    run(code.read("src/a.py"))
    assert reads == ["src/a.py"]  # cached
    out = run(code.search("a\\.py"))
    assert "src/a.py" in out
    assert "search_code only matches file paths" in run(code.overview())


def test_overview_of_empty_repo():
    assert build_overview([], {}).startswith("0 text files")
