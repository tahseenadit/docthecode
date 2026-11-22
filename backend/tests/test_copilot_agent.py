"""The Copilot adapter against a stand-in for the Copilot SDK (the real one needs a Copilot-enabled org)."""
import asyncio
import sys
import types

import pytest

from app.agent.copilot_agent import copilot_ping, run_with_copilot
from app.agent.codebase import SnapshotCodebase
from app.agent.doc_agent import TOOL_NAMES, CodeContext, DocWorkspace
from app.agent.providers import ProviderError
from app.gitstore import Author, GitStore

CTX = CodeContext("acme/api", 7, "Add --verbose", "", "ada", "main", "f" * 40, [], "+--verbose")


def fake_sdk(script, record):
    """`script(tools)` plays the model: it calls tool handlers like Copilot would."""
    mod = types.ModuleType("copilot")

    class Tool:
        def __init__(self, name, description, parameters, handler):
            self.name, self.handler = name, handler

    class Session:
        def __init__(self, config):
            self.config, self.listeners = config, []

        def on(self, fn):
            self.listeners.append(fn)
            return lambda: None

        async def send_and_wait(self, message, timeout=None):
            record["prompts"].append(message["prompt"])
            tools = {t.name: t for t in self.config["tools"]}
            for fn in self.listeners:
                fn(types.SimpleNamespace(type="assistant.message", data=types.SimpleNamespace(content="Working on it")))
            await script(tools, len(record["prompts"]))

    class Client:
        def __init__(self, connection=None, github_token=None, use_logged_in_user=True):
            record["token"], record["logged_in"] = github_token, use_logged_in_user

        async def start(self):
            record["started"] = True

        async def stop(self):
            record["stopped"] = True

        async def create_session(self, config):
            record["config"] = config
            return Session(config)

    mod.CopilotClient = Client
    mod.RuntimeConnection = types.SimpleNamespace(for_stdio=lambda: "stdio")
    mod.PermissionHandler = types.SimpleNamespace(approve_all=lambda *a: True)
    mod.define_tool = lambda name, description, parameters, handler: Tool(name, description, parameters, handler)
    return mod


@pytest.fixture
def ws(tmp_path):
    store = GitStore(tmp_path)
    store.init_repo("d", "main", Author("a", "a@x"), {"cli.md": "# CLI\n"})
    return DocWorkspace(lambda: store.list_files("d", "main"), lambda p: store.read_file("d", "main", p))


CODE = SnapshotCodebase({"cli.py": "parser.add_argument('--verbose')\n"})


def test_copilot_run_uses_installation_token_and_only_docthecode_tools(ws, monkeypatch):
    record = {"prompts": []}

    async def model(tools, turn):
        assert "# CLI" in await tools["read_doc_file"].handler({"path": "cli.md"}, None)
        assert "cli.py:1:" in await tools["search_code"].handler({"pattern": "verbose"}, None)
        assert (await tools["write_doc_file"].handler({"path": "../x", "content": "x"}, None)).startswith("Error")
        await tools["write_doc_file"].handler({"path": "cli.md", "content": "# CLI\n\n--verbose\n"}, None)
        await tools["finish"].handler({"summary": "Documented --verbose", "changes_needed": True}, None)

    monkeypatch.setitem(sys.modules, "copilot", fake_sdk(model, record))
    res = asyncio.run(run_with_copilot(CTX, ws, CODE, github_token="ghs_install", model="auto"))
    assert res.finished and res.summary == "Documented --verbose"
    assert res.changes == {"cli.md": "# CLI\n\n--verbose\n"}
    assert record["token"] == "ghs_install" and record["logged_in"] is False and record["stopped"]
    assert record["config"]["available_tools"] == TOOL_NAMES
    assert "bash" not in TOOL_NAMES and "search_code" in record["config"]["system_message"]["content"]
    assert "--verbose" in record["prompts"][0] and len(record["prompts"]) == 1
    assert any(t.startswith("💬") for t in res.transcript) and res.mode == "bootstrap"
    assert "## Mode: bootstrap" in record["prompts"][0]


def test_copilot_is_nudged_to_finish(ws, monkeypatch):
    record = {"prompts": []}

    async def model(tools, turn):
        if turn == 2:
            await tools["finish"].handler({"summary": "Nothing to change", "changes_needed": False}, None)

    monkeypatch.setitem(sys.modules, "copilot", fake_sdk(model, record))
    res = asyncio.run(run_with_copilot(CTX, ws, CODE, github_token="t"))
    assert res.finished and len(record["prompts"]) == 2 and "finish" in record["prompts"][1]


def test_copilot_ping(monkeypatch):
    async def calls_ping(tools, turn):
        await tools["ping"].handler({}, None)

    async def ignores(tools, turn):
        pass

    monkeypatch.setitem(sys.modules, "copilot", fake_sdk(calls_ping, {"prompts": []}))
    asyncio.run(copilot_ping("t"))
    monkeypatch.setitem(sys.modules, "copilot", fake_sdk(ignores, {"prompts": []}))
    with pytest.raises(ProviderError):
        asyncio.run(copilot_ping("t"))


def test_missing_sdk_is_a_clear_error(ws, monkeypatch):
    monkeypatch.setitem(sys.modules, "copilot", None)
    with pytest.raises(ProviderError, match="github-copilot-sdk"):
        asyncio.run(run_with_copilot(CTX, ws, CODE, github_token="t"))
