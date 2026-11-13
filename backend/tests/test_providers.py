"""The agent loop through each provider's wire format, with recorded fake HTTP."""
import asyncio
import copy
import json

import pytest

from app.agent.doc_agent import CodeContext, DocAgent, DocWorkspace
from app.agent.providers import (AgentConfig, AnthropicMessages, OpenAIChat, ProviderError, from_openai_response,
                                 list_models, messages_api, to_openai_messages)
from app.gitstore import Author, GitStore

CTX = CodeContext("acme/api", 7, "Add --verbose", "", "ada", "main", "f" * 40,
                  [{"filename": "cli.py", "status": "modified"}], "+--verbose")


def ws_for(tmp_path):
    store = GitStore(tmp_path)
    store.init_repo("d", "main", Author("a", "a@x"), {"cli.md": "# CLI\n"})
    return DocWorkspace(lambda: store.list_files("d", "main"), lambda p: store.read_file("d", "main", p))


class FakeHTTP:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def __call__(self, url, headers, body):
        self.calls.append((url, headers, copy.deepcopy(body)))
        return self.replies.pop(0)


def openai_reply(content=None, calls=(), finish="stop"):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = [{"id": cid, "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
                             for cid, n, a in calls]
        finish = "tool_calls"
    return {"choices": [{"message": msg, "finish_reason": finish}]}


async def nothing(*_):
    return None


def test_full_agent_run_over_openai_wire_format(tmp_path):
    http = FakeHTTP([
        openai_reply("Reading the CLI page.", [("c1", "read_doc_file", {"path": "cli.md"})]),
        openai_reply(None, [("c2", "write_doc_file", {"path": "cli.md", "content": "# CLI\n\n--verbose\n"}),
                            ("c3", "finish", {"summary": "Documented --verbose", "changes_needed": True})]),
    ])
    api = OpenAIChat("sk-test", "https://api.openai.com/v1", post=http)
    res = asyncio.run(DocAgent(api, "some-model").run(CTX, ws_for(tmp_path), nothing, nothing))
    assert res.finished and res.changes == {"cli.md": "# CLI\n\n--verbose\n"}

    url, headers, body = http.calls[0]
    assert url == "https://api.openai.com/v1/chat/completions" and headers["Authorization"] == "Bearer sk-test"
    assert body["messages"][0]["role"] == "system" and body["tools"][0]["type"] == "function"
    # second request carries the assistant tool call and its result in OpenAI's shape
    msgs = http.calls[1][2]["messages"]
    assert msgs[-2]["tool_calls"][0]["function"]["name"] == "read_doc_file"
    assert msgs[-1] == {"role": "tool", "tool_call_id": "c1", "content": "# CLI\n"}


def test_anthropic_is_a_pass_through(tmp_path):
    http = FakeHTTP([{"content": [{"type": "tool_use", "id": "t1", "name": "finish",
                                   "input": {"summary": "ok", "changes_needed": False}}], "stop_reason": "tool_use"}])
    api = AnthropicMessages("sk-ant-x", post=http)
    res = asyncio.run(DocAgent(api, "claude-sonnet-5-5").run(CTX, ws_for(tmp_path), nothing, nothing))
    url, headers, body = http.calls[0]
    assert url == "https://api.anthropic.com/v1/messages" and headers["x-api-key"] == "sk-ant-x"
    assert body["tools"][0]["input_schema"] and res.finished


def test_tool_errors_and_bad_json_survive_translation():
    msgs = to_openai_messages("sys", [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "x", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "boom", "is_error": True}]},
    ])
    assert msgs[-1] == {"role": "tool", "tool_call_id": "a", "content": "Error: boom"}
    r = from_openai_response({"choices": [{"message": {"tool_calls": [
        {"id": "z", "function": {"name": "read_doc_file", "arguments": "{not json"}}]}, "finish_reason": "tool_calls"}]})
    assert r.stop_reason == "tool_use" and "_invalid_json" in r.content[0]["input"]
    assert from_openai_response(openai_reply("hi")).stop_reason == "end_turn"
    with pytest.raises(ProviderError):
        from_openai_response({"choices": []})


def test_falls_back_to_max_tokens_for_older_servers():
    calls = []

    async def post(url, headers, body):
        calls.append(dict(body))
        if "max_completion_tokens" in body:
            raise ProviderError("400: unknown parameter max_completion_tokens")
        return openai_reply("ok")

    asyncio.run(OpenAIChat("", "http://ollama:11434/v1", post=post).create(
        model="m", max_tokens=10, system="s", tools=[], messages=[{"role": "user", "content": "x"}]))
    assert "max_tokens" in calls[1] and "Authorization" not in str(calls)


def test_provider_selection_and_validation():
    assert isinstance(messages_api(AgentConfig("gemini", "k", "gemini-x", None)), OpenAIChat)
    assert messages_api(AgentConfig("gemini", "k", "m", None)).base.endswith("/v1beta/openai")
    assert AgentConfig(None, None, None, None).problem().startswith("No AI provider")
    assert "API key" in AgentConfig("openai", None, "m", None).problem()
    assert "model" in AgentConfig("openai", "k", None, None).problem()
    assert AgentConfig("anthropic", "k", None, None).problem() is None  # has a default model
    assert "base URL" in AgentConfig("openai_compatible", None, "m", None).problem()
    assert AgentConfig("copilot", None, None, None).problem() is None  # no key, model "auto"


def test_list_models_normalizes_ids():
    async def get(url, headers):
        return {"data": [{"id": "models/gemini-3.8-flash"}, {"id": "models/gemini-3.8-pro"}]}

    got = asyncio.run(list_models(AgentConfig("gemini", "k", None, None), get=get))
    assert got == ["gemini-3.8-flash", "gemini-3.8-pro"]
