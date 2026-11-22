import copy
import asyncio
from types import SimpleNamespace as NS

from app.agent.codebase import SnapshotCodebase
from app.agent.doc_agent import (CodeContext, DocAgent, DocWorkspace, SYSTEM_PROMPT, TOOL_NAMES, build_task_message,
                                 compact, plan_run)
from app.gitstore import Author, GitStore


class ScriptedLLM:
    """Replays a fixed sequence of assistant turns and records what it was sent."""

    def __init__(self, turns):
        self.turns, self.calls = list(turns), []

    async def create(self, **kw):
        self.calls.append(copy.deepcopy(kw))  # the agent keeps appending to the same list
        content, stop = self.turns.pop(0)
        return NS(content=content, stop_reason=stop)


def tool(id, name, **inp):
    return {"type": "tool_use", "id": id, "name": name, "input": inp}


CTX = CodeContext("acme/api", 7, "Add --verbose flag", "Adds a verbose flag to the CLI.", "ada", "main",
                  "f" * 40, [{"filename": "cli.py", "status": "modified", "additions": 3, "deletions": 0}],
                  "+parser.add_argument('--verbose')")

WORDS = " ".join(["The command line tool prints a report of every invoice it processed."] * 12)
SPACE = {
    "index.md": "# Acme\n\n" + WORDS + "\n",
    "reference/cli.md": "---\ntype: reference\ncovers: cli.py\n---\n# CLI\n\nFlags: --quiet\n\n" + WORDS + "\n",
    "business/invoices.md": "---\ntype: business\ncovers: billing/*\n---\n# Invoices\n\n" + WORDS + "\n",
    "old.md": "x",
}
CODE = SnapshotCodebase({"cli.py": "import argparse\nparser.add_argument('--quiet')\nparser.add_argument('--verbose')\n",
                         "billing/tax.py": "RATE = 0.25\n", "README.md": "# Acme API\n"})


def make_ws(tmp_path, files=None):
    store = GitStore(tmp_path)
    store.init_repo("docs", "main", Author("a", "a@x"), files if files is not None else SPACE)
    return DocWorkspace(lambda: store.list_files("docs", "main"), lambda p: store.read_file("docs", "main", p))


def run(coro):
    return asyncio.run(coro)


def test_agent_edits_and_finishes(tmp_path):
    ws = make_ws(tmp_path)
    new_cli = "---\ntype: reference\ncovers: cli.py\n---\n# CLI\n\nFlags: --quiet, --verbose\n"
    llm = ScriptedLLM([
        ([{"type": "text", "text": "Looking at docs"}, tool("1", "list_doc_files"),
          tool("2", "read_doc_file", path="reference/cli.md")], "tool_use"),
        ([tool("3", "read_code_file", path="cli.py"),
          tool("4", "write_doc_file", path="reference/cli.md", content=new_cli),
          tool("5", "delete_doc_file", path="old.md")], "tool_use"),
        ([tool("6", "finish", summary="Documented `--verbose`.", changes_needed=True)], "tool_use"),
    ])
    res = run(DocAgent(llm, "test-model").run(CTX, ws, CODE, feedback=["Mention short flag -v"]))
    assert res.finished and res.turns == 3 and res.mode == "update"
    assert res.changes == {"reference/cli.md": new_cli, "old.md": None}
    assert res.summary == "Documented `--verbose`."
    first = llm.calls[0]["messages"][0]["content"]
    assert "--verbose" in first and "Mention short flag -v" in first and "reference/cli.md" in first
    # tool results flow back with matching ids
    results = llm.calls[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["1", "2"]
    assert "Flags: --quiet" in results[1]["content"]
    assert "--verbose" in llm.calls[2]["messages"][-1]["content"][0]["content"]  # read_code_file


def test_update_mode_lists_related_pages_and_missing_kinds(tmp_path):
    ws = make_ws(tmp_path)
    plan = plan_run(CTX, ws)
    assert plan.mode == "update"
    assert [p.path for p, _ in plan.related] == ["reference/cli.md"]
    assert set(plan.status.missing) == {"tutorial", "technical"}
    msg = build_task_message(CTX, plan, ws.outline(), "OVERVIEW")
    assert "## Mode: update" in msg and "No pages yet for: Tutorials, Technical" in msg
    assert "- reference/cli.md (reference): covers cli.py" in msg
    assert "## Documentation inventory" in msg and "business/invoices.md · business" in msg
    assert "OVERVIEW" in msg


def test_bootstrap_mode_for_an_empty_space(tmp_path):
    ws = make_ws(tmp_path, {"index.md": "# Docs\n\nComing soon.\n"})
    llm = ScriptedLLM([
        ([tool("1", "code_overview"), tool("2", "search_code", pattern="add_argument")], "tool_use"),
        ([tool("3", "write_doc_file", path="tutorials/getting-started.md", content="# Getting started\n")], "tool_use"),
        ([tool("4", "finish", summary="Wrote the docs.", changes_needed=True)], "tool_use"),
    ])
    res = run(DocAgent(llm, "m", max_turns=2, bootstrap_max_turns=10).run(CTX, ws, CODE))
    assert res.finished and res.mode == "bootstrap" and res.turns == 3   # bootstrap gets the larger turn budget
    first = llm.calls[0]["messages"][0]["content"]
    assert "## Mode: bootstrap" in first and "README.md" in first  # code overview is in the task
    outs = llm.calls[1]["messages"][-1]["content"]
    assert "3 text files" in outs[0]["content"] and "cli.py:2:" in outs[1]["content"]
    note = llm.calls[2]["messages"][-1]["content"][0]["content"]
    assert "no `type:` front matter" in note
    assert res.transcript[0].startswith("📋 Mode: bootstrap")


def test_search_docs_and_inventory_tools(tmp_path):
    ws = make_ws(tmp_path)
    llm = ScriptedLLM([
        ([tool("1", "search_docs", pattern="--QUIET"), tool("2", "doc_inventory")], "tool_use"),
        ([tool("3", "finish", summary="ok", changes_needed=False)], "tool_use"),
    ])
    run(DocAgent(llm, "m").run(CTX, ws, CODE))
    out = llm.calls[1]["messages"][-1]["content"]
    assert "reference/cli.md:7: Flags: --quiet" in out[0]["content"]
    assert "Business: 1 page(s)" in out[1]["content"] and "covers: billing/*" in out[1]["content"]


def test_tool_errors_are_returned_to_model(tmp_path):
    ws = make_ws(tmp_path)
    llm = ScriptedLLM([
        ([tool("1", "write_doc_file", path="../etc/passwd", content="x")], "tool_use"),
        ([tool("2", "finish", summary="No changes", changes_needed=False)], "tool_use"),
    ])
    res = run(DocAgent(llm, "m").run(CTX, ws, None))
    err = llm.calls[1]["messages"][-1]["content"][0]
    assert err["is_error"] and "Invalid file path" in err["content"]
    assert res.changes == {}


def test_code_tools_without_a_codebase(tmp_path):
    ws = make_ws(tmp_path)
    llm = ScriptedLLM([([tool("1", "read_code_file", path="cli.py")], "tool_use"),
                       ([tool("2", "finish", summary="ok", changes_needed=False)], "tool_use")])
    run(DocAgent(llm, "m").run(CTX, ws, None))
    assert "not available" in llm.calls[1]["messages"][-1]["content"][0]["content"]


def test_nudges_when_model_stops_without_finish(tmp_path):
    ws = make_ws(tmp_path)
    llm = ScriptedLLM([
        ([{"type": "text", "text": "Done!"}], "end_turn"),
        ([tool("1", "finish", summary="ok", changes_needed=False)], "tool_use"),
    ])
    res = run(DocAgent(llm, "m").run(CTX, ws, None))
    assert res.finished and "finish" in llm.calls[1]["messages"][-1]["content"]


def test_turn_limit(tmp_path):
    full = {**SPACE, "tutorials/start.md": "---\ntype: tutorial\n---\n# Start\n" + WORDS,
            "technical/arch.md": "---\ntype: technical\n---\n# Architecture\n" + WORDS}
    ws = make_ws(tmp_path, full)   # nothing missing: the normal turn budget applies
    llm = ScriptedLLM([([tool(str(i), "list_doc_files")], "tool_use") for i in range(3)])
    res = run(DocAgent(llm, "m", max_turns=3).run(CTX, ws, None))
    assert not res.finished and res.turns == 3


def test_workspace_write_back_to_original_is_noop(tmp_path):
    ws = make_ws(tmp_path)
    ws.write("old.md", "changed")
    ws.write("old.md", "x")
    ws.write("new.md", "n")
    ws.delete("new.md")
    assert ws.changes == {}


def test_incremental_message_label(tmp_path):
    ctx = CodeContext(**{**CTX.__dict__, "incremental": True})
    ws = make_ws(tmp_path)
    assert "since your last run" in build_task_message(ctx, plan_run(ctx, ws), "")


def test_agent_sees_page_tree_not_code_layout(tmp_path):
    ws = make_ws(tmp_path)
    llm = ScriptedLLM([([tool("1", "list_pages"), tool("2", "finish", summary="ok", changes_needed=False)], "tool_use")])
    run(DocAgent(llm, "m").run(CTX, ws, None))
    first = llm.calls[0]["messages"][0]["content"]
    assert "page tree" in first and "CLI (reference/cli.md)" in first
    assert "not a copy of the code" in llm.calls[0]["system"]


def test_prompt_covers_the_four_kinds():
    for kind in ("**Business**", "**Tutorials**", "**Reference**", "**Technical**", "covers:", "Bootstrap mode"):
        assert kind in SYSTEM_PROMPT
    assert {"search_code", "code_overview", "search_docs", "doc_inventory"} <= set(TOOL_NAMES)


def test_compact_drops_oldest_large_outputs_but_keeps_the_task_and_recent_turns():
    big = "y" * 5000
    msgs = [{"role": "user", "content": "TASK " + big}]
    for i in range(10):
        msgs.append({"role": "assistant", "content": [tool(str(i), "write_doc_file", path=f"p{i}.md", content=big)]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(i), "content": big}]})
    dropped = compact(msgs, budget=40_000, keep_last=4)
    assert dropped > 0
    assert msgs[0]["content"].startswith("TASK") and len(msgs[0]["content"]) > 5000
    assert msgs[1]["content"][0]["input"]["content"].startswith("<5000 characters")
    assert msgs[2]["content"][0]["content"].startswith("[Earlier output removed")
    assert msgs[-1]["content"][0]["content"] == big and msgs[-2]["content"][0]["input"]["content"] == big
    assert compact(msgs, budget=10**9) == 0
