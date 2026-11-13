"""Model providers for the documentation agent.

The agent loop (doc_agent.py) speaks one neutral format: messages whose content is text or
blocks of `text`, `tool_use {id, name, input}` and `tool_result {tool_use_id, content, is_error}`.
Each adapter below translates that to a provider's API and back, using plain HTTPS:

  anthropic           Anthropic Messages API
  openai              OpenAI Chat Completions with function calling
  gemini              Google Gemini through its OpenAI-compatible endpoint
  openai_compatible   Any OpenAI-compatible endpoint (Azure AI Foundry, OpenRouter, Ollama, vLLM, ...)
  copilot             GitHub Copilot through the Copilot SDK (see copilot_agent.py): billed to the
                      organization's Copilot plan via DocTheCode's GitHub App, no API key

Models are not hard-coded (they change often): admins pick from the list the provider returns.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

Post = Callable[[str, dict, dict], Awaitable[dict]]
Get = Callable[[str, dict], Awaitable[dict]]


@dataclass(frozen=True)
class ProviderInfo:
    id: str
    label: str
    base_url: str              # default; editable for openai_compatible (and overridable for others)
    needs_key: bool
    default_model: str | None
    key_hint: str
    key_url: str | None


PROVIDERS: dict[str, ProviderInfo] = {p.id: p for p in [
    ProviderInfo("anthropic", "Anthropic (Claude)", "https://api.anthropic.com", True, "claude-sonnet-5-5",
                 "sk-ant-…", "https://console.anthropic.com/settings/keys"),
    ProviderInfo("openai", "OpenAI", "https://api.openai.com/v1", True, None,
                 "sk-…", "https://platform.openai.com/api-keys"),
    ProviderInfo("gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", True, None,
                 "Gemini API key", "https://aistudio.google.com/apikey"),
    ProviderInfo("copilot", "GitHub Copilot", "", False, "auto",
                 "No key: uses DocTheCode's GitHub App, billed to your organization's Copilot plan", None),
    ProviderInfo("openai_compatible", "Other (OpenAI-compatible)", "", True, None,
                 "API key for that endpoint (leave empty if none)", None),
]}


class ProviderError(RuntimeError):
    pass


async def _http_post(url: str, headers: dict, body: dict) -> dict:
    import httpx

    async with httpx.AsyncClient(timeout=180) as http:
        r = await http.post(url, headers=headers, json=body)
    if r.status_code >= 400:
        raise ProviderError(f"{r.status_code} from {url.split('/')[2]}: {r.text[:500]}")
    return r.json()


async def _http_get(url: str, headers: dict) -> dict:
    import httpx

    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.get(url, headers=headers)
    if r.status_code >= 400:
        raise ProviderError(f"{r.status_code} from {url.split('/')[2]}: {r.text[:500]}")
    return r.json()


# ------------------------------------------------------------------ Anthropic
class AnthropicMessages:
    """The neutral format is the Anthropic format, so this is a thin pass-through."""

    def __init__(self, api_key: str, base_url: str = "", post: Post | None = None):
        self.api_key, self.base = api_key, (base_url or PROVIDERS["anthropic"].base_url).rstrip("/")
        self._post = post or _http_post

    async def create(self, *, model: str, max_tokens: int, system: str, tools: list[dict], messages: list[dict]):
        data = await self._post(f"{self.base}/v1/messages",
                                {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
                                {"model": model, "max_tokens": max_tokens, "system": system, "tools": tools,
                                 "messages": messages})
        return SimpleNamespace(content=data.get("content", []), stop_reason=data.get("stop_reason"))


# ------------------------------------------------------------------ OpenAI and compatible
def to_openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
            for t in tools]


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}] if system else []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        if m["role"] == "assistant":
            text = "".join(b.get("text", "") for b in content if b["type"] == "text")
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                     for b in content if b["type"] == "tool_use"]
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        else:
            for b in content:
                if b["type"] == "tool_result":
                    body = b.get("content", "")
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                                "content": ("Error: " + body) if b.get("is_error") and not body.startswith("Error") else body})
                elif b["type"] == "text":
                    out.append({"role": "user", "content": b["text"]})
    return out


def from_openai_response(data: dict) -> SimpleNamespace:
    choices = data.get("choices") or []
    if not choices:
        raise ProviderError(f"No answer from the model: {json.dumps(data)[:300]}")
    msg, finish = choices[0].get("message") or {}, choices[0].get("finish_reason")
    blocks: list[dict] = []
    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})
    for i, call in enumerate(msg.get("tool_calls") or []):
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"_invalid_json": fn.get("arguments", "")}
        blocks.append({"type": "tool_use", "id": call.get("id") or f"call_{i}", "name": fn.get("name", ""),
                       "input": args if isinstance(args, dict) else {"value": args}})
    has_calls = any(b["type"] == "tool_use" for b in blocks)
    stop = "tool_use" if has_calls else ("max_tokens" if finish == "length" else "end_turn")
    return SimpleNamespace(content=blocks, stop_reason=stop)


class OpenAIChat:
    def __init__(self, api_key: str, base_url: str, post: Post | None = None):
        if not base_url:
            raise ProviderError("This provider needs an API base URL")
        self.api_key, self.base = api_key, base_url.rstrip("/")
        self._post = post or _http_post

    async def create(self, *, model: str, max_tokens: int, system: str, tools: list[dict], messages: list[dict]):
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {"model": model, "messages": to_openai_messages(system, messages), "tools": to_openai_tools(tools),
                "tool_choice": "auto", "max_completion_tokens": max_tokens}
        try:
            data = await self._post(f"{self.base}/chat/completions", headers, body)
        except ProviderError as e:
            if "max_completion_tokens" not in str(e):
                raise
            body["max_tokens"] = body.pop("max_completion_tokens")  # older OpenAI-compatible servers
            data = await self._post(f"{self.base}/chat/completions", headers, body)
        return from_openai_response(data)


# ------------------------------------------------------------------ factory, model lists
@dataclass(frozen=True)
class AgentConfig:
    provider: str | None
    api_key: str | None
    model: str | None
    base_url: str | None

    @property
    def info(self) -> ProviderInfo | None:
        return PROVIDERS.get(self.provider or "")

    @property
    def effective_base_url(self) -> str:
        return (self.base_url or (self.info.base_url if self.info else "")).rstrip("/")

    @property
    def effective_model(self) -> str | None:
        return self.model or (self.info.default_model if self.info else None)

    def problem(self) -> str | None:
        """Why the agent can't run with this configuration, or None."""
        if not self.info:
            return "No AI provider is configured. An admin can choose one under Admin → Documentation agent."
        if self.info.needs_key and not self.api_key and self.provider != "openai_compatible":
            return f"No API key is configured for {self.info.label}."
        if self.provider == "openai_compatible" and not self.base_url:
            return "No API base URL is configured for the OpenAI-compatible provider."
        if not self.effective_model:
            return f"No model is selected for {self.info.label}."
        return None


def messages_api(cfg: AgentConfig, post: Post | None = None):
    """Chat adapter for the agent loop (all providers except Copilot, which runs its own loop)."""
    if cfg.provider == "anthropic":
        return AnthropicMessages(cfg.api_key or "", cfg.base_url or "", post)
    if cfg.provider in ("openai", "gemini", "openai_compatible"):
        return OpenAIChat(cfg.api_key or "", cfg.effective_base_url, post)
    raise ProviderError(f"Provider {cfg.provider!r} has no chat adapter")


async def list_models(cfg: AgentConfig, get: Get | None = None) -> list[str]:
    get = get or _http_get
    if cfg.provider == "copilot":
        return ["auto"]  # Copilot picks a model the organization's policy allows; specific names also work
    if cfg.provider == "anthropic":
        data = await get(f"{cfg.effective_base_url}/v1/models?limit=100",
                         {"x-api-key": cfg.api_key or "", "anthropic-version": "2023-06-01"})
    else:
        data = await get(f"{cfg.effective_base_url}/models",
                         {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {})
    ids = [m.get("id", "") for m in data.get("data", data.get("models", []))]
    return sorted({i.removeprefix("models/") for i in ids if i})
