"""Run the documentation agent on GitHub Copilot, through the Copilot SDK.

Copilot runs its own agent loop. DocTheCode gives it the same tools as every other provider and
allows only those (no shell, no file system), so it can only read and edit the doc space
through DocTheCode, exactly like the other providers.

Authentication is server-to-server: DocTheCode's GitHub App installation token for the code
repo's organization. Requirements (shown to admins in the UI):
  * the GitHub App has the organization permission "Copilot Requests" (read-only),
  * the organization allows Copilot requests from GitHub App installations,
  * the organization's Copilot policy allows the chosen model ("auto" lets Copilot choose).
Usage is billed to that organization's Copilot plan.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from .doc_agent import (SYSTEM_PROMPT, TOOLS, AgentResult, CodeContext, DocWorkspace, _short_args,
                        build_task_message, call_tool)
from .providers import ProviderError

COPILOT_PREAMBLE = """You are running inside DocTheCode, not in a code checkout. Use ONLY the DocTheCode tools you were given \
(list_pages, read_doc_file, write_doc_file, delete_doc_file, list_code_files, read_code_file, finish). \
There is no shell and no local file system.

"""


def _sdk():
    try:
        import copilot  # package: github-copilot-sdk
    except ImportError as e:
        raise ProviderError("The GitHub Copilot SDK is not installed in this DocTheCode image (pip package "
                            "github-copilot-sdk).") from e
    return copilot


def _client(copilot, github_token: str):
    """Create a Copilot client authenticated with a GitHub App installation token (no logged-in user)."""
    if hasattr(copilot, "RuntimeConnection"):
        return copilot.CopilotClient(connection=copilot.RuntimeConnection.for_stdio(),
                                     github_token=github_token, use_logged_in_user=False)
    return copilot.CopilotClient({"github_token": github_token, "use_logged_in_user": False})  # older SDKs


async def run_with_copilot(ctx: CodeContext, ws: DocWorkspace,
                           read_code: Callable[[str], Awaitable[str | None]],
                           list_code: Callable[[], Awaitable[list[str]]], *,
                           github_token: str, model: str = "auto", instructions: str = "",
                           feedback: list[str] | None = None, existing_doc_diff: str = "",
                           timeout: float = 900) -> AgentResult:
    copilot = _sdk()
    transcript: list[str] = []
    state = {"summary": "", "finished": False, "calls": 0}

    def make_handler(name: str):
        async def handler(args, invocation=None):
            args = args if isinstance(args, dict) else (getattr(args, "arguments", None) or {})
            state["calls"] += 1
            if name == "finish":
                state["summary"], state["finished"] = str(args.get("summary", "")).strip(), True
                transcript.append(f"🔧 finish({_short_args(args)})")
                return "Finished. Stop now."
            try:
                out = await call_tool(name, args, ws, read_code, list_code)
                transcript.append(f"🔧 {name}({_short_args(args)})")
                return out
            except Exception as e:  # tool errors go back to the model
                transcript.append(f"🔧 {name}({_short_args(args)}) → error")
                return f"Error: {e}"
        return handler

    tools = [copilot.define_tool(name=t["name"], description=t["description"], parameters=t["input_schema"],
                                 handler=make_handler(t["name"])) for t in TOOLS]
    task = build_task_message(ctx, ws.outline(), instructions, feedback or [], existing_doc_diff)

    client = _client(copilot, github_token)
    await client.start()
    try:
        session = await client.create_session({
            "model": model or "auto",
            "system_message": {"mode": "append", "content": COPILOT_PREAMBLE + SYSTEM_PROMPT},
            "tools": tools,
            "available_tools": [t["name"] for t in TOOLS],  # only DocTheCode's tools: no shell, no files
            "on_permission_request": copilot.PermissionHandler.approve_all,
        })

        def on_event(event):
            if getattr(event, "type", "") == "assistant.message":
                text = (getattr(event.data, "content", "") or "").strip()
                if text:
                    transcript.append(f"💬 {text[:2000]}")
            elif getattr(event, "type", "") == "session.error":
                transcript.append(f"⚠️ {getattr(event.data, 'message', 'Copilot error')}")

        unsubscribe = session.on(on_event)
        try:
            await asyncio.wait_for(session.send_and_wait({"prompt": task}, timeout=timeout), timeout + 30)
            if not state["finished"]:
                await asyncio.wait_for(session.send_and_wait(
                    {"prompt": "Call the `finish` tool now with a summary of your documentation changes."},
                    timeout=120), 150)
        finally:
            unsubscribe()
    except asyncio.TimeoutError:
        transcript.append("⚠️ Copilot did not finish in time.")
    finally:
        try:
            await client.stop()
        except Exception:
            pass

    return AgentResult(dict(ws.changes), state["summary"] or "Agent stopped before finishing.", state["finished"],
                       state["calls"], transcript)


async def copilot_ping(github_token: str, model: str = "auto", timeout: float = 120) -> None:
    """Connectivity check for the admin page: Copilot must call a custom tool once."""
    copilot = _sdk()
    called = asyncio.Event()

    async def ping(args, invocation=None):
        called.set()
        return "pong"

    tool = copilot.define_tool(name="ping", description="Reply to a connectivity check.",
                               parameters={"type": "object", "properties": {}}, handler=ping)
    client = _client(copilot, github_token)
    await client.start()
    try:
        session = await client.create_session({
            "model": model or "auto", "tools": [tool], "available_tools": ["ping"],
            "on_permission_request": copilot.PermissionHandler.approve_all,
        })
        await asyncio.wait_for(session.send_and_wait({"prompt": "Call the ping tool once, then stop."},
                                                     timeout=timeout), timeout + 15)
    except asyncio.TimeoutError:
        raise ProviderError("GitHub Copilot did not answer in time.")
    except ProviderError:
        raise
    except Exception as e:  # SDK / auth / policy errors: make them readable for the admin
        raise ProviderError(f"GitHub Copilot: {e}. Check that DocTheCode's GitHub App has the organization permission "
                            "'Copilot Requests' and that the organization allows Copilot requests from GitHub Apps.")
    finally:
        try:
            await client.stop()
        except Exception:
            pass
    if not called.is_set():
        raise ProviderError("GitHub Copilot answered but did not call the tool.")
