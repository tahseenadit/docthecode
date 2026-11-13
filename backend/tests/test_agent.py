import copy
import asyncio
from types import SimpleNamespace as NS

from app.agent.doc_agent import CodeContext, DocAgent, DocWorkspace, build_task_message
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


def make_ws(tmp_path):
    store = GitStore(tmp_path)
    store.init_repo("docs", "main", Author("a", "a@x"), {"cli.md": "# CLI\n\nFlags: --quiet\n", "old.md": "x"})
    return DocWorkspace(lambda: store.list_files("docs", "main"), lambda p: store.read_file("docs", "main", p))


def run(coro):
    return asyncio.run(coro)


def test_agent_edits_and_finishes(tmp_path):
    ws = make_ws(tmp_path)
    llm = ScriptedLLM([
        ([{"type": "text", "text": "Looking at docs"}, tool("1", "list_doc_files"), tool("2", "read_doc_file", path="cli.md")],
         "tool_use"),
        ([tool("3", "read_code_file", path="cli.py"),
          tool("4", "write_doc_file", path="cli.md", content="# CLI\n\nFlags: --quiet, --verbose\n"),
          tool("5", "delete_doc_file", path="old.md")], "tool_use"),
        ([tool("6", "finish", summary="Documented `--verbose`.", changes_needed=True)], "tool_use"),
    ])

    async def read_code(p):
        return "code" if p == "cli.py" else None

    async def list_code():
        return ["cli.py"]

    res = run(DocAgent(llm, "test-model").run(CTX, ws, read_code, list_code, feedback=["Mention short flag -v"]))
    assert res.finished and res.turns == 3
    assert res.changes == {"cli.md": "# CLI\n\nFlags: --quiet, --verbose\n", "old.md": None}
    assert res.summary == "Documented `--verbose`."
    first = llm.calls[0]["messages"][0]["content"]
    assert "--verbose" in first and "Mention short flag -v" in first and "cli.md" in first
    # tool results flow back with matching ids
    results = llm.calls[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["1", "2"]
    assert "Flags: --quiet" in results[1]["content"]


def test_tool_errors_are_returned_to_model(tmp_path):
    ws = make_ws(tmp_path)
    llm = ScriptedLLM([
        ([tool("1", "write_doc_file", path="../etc/passwd", content="x")], "tool_use"),
        ([tool("2", "finish", summary="No changes", changes_needed=False)], "tool_use"),
    ])

    async def nothing(*_):
        return None

    res = run(DocAgent(llm, "m").run(CTX, ws, nothing, nothing))
    err = llm.calls[1]["messages"][-1]["content"][0]
    assert err["is_error"] and "Invalid file path" in err["content"]
    assert res.changes == {}


def test_nudges_when_model_stops_without_finish(tmp_path):
    ws = make_ws(tmp_path)
    llm = ScriptedLLM([
        ([{"type": "text", "text": "Done!"}], "end_turn"),
        ([tool("1", "finish", summary="ok", changes_needed=False)], "tool_use"),
    ])
    res = run(DocAgent(llm, "m").run(CTX, ws, None, None))
    assert res.finished and "finish" in llm.calls[1]["messages"][-1]["content"]


def test_turn_limit(tmp_path):
    ws = make_ws(tmp_path)
    llm = ScriptedLLM([([tool(str(i), "list_doc_files")], "tool_use") for i in range(3)])
    res = run(DocAgent(llm, "m", max_turns=3).run(CTX, ws, None, None))
    assert not res.finished and res.turns == 3


def test_workspace_write_back_to_original_is_noop(tmp_path):
    ws = make_ws(tmp_path)
    ws.write("cli.md", "changed")
    ws.write("cli.md", "# CLI\n\nFlags: --quiet\n")
    ws.write("new.md", "n")
    ws.delete("new.md")
    assert ws.changes == {}


def test_incremental_message_label():
    ctx = CodeContext(**{**CTX.__dict__, "incremental": True})
    assert "since your last run" in build_task_message(ctx, "", "", [], "")


def test_agent_sees_page_tree_not_code_layout(tmp_path):
    ws = make_ws(tmp_path)
    llm = ScriptedLLM([([tool("1", "list_pages"), tool("2", "finish", summary="ok", changes_needed=False)], "tool_use")])
    run(DocAgent(llm, "m").run(CTX, ws, None, None))
    first = llm.calls[0]["messages"][0]["content"]
    assert "page tree" in first and "- CLI (cli.md)" in first
    assert "NOT a mirror of the" in llm.calls[0]["system"]
