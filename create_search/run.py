"""Entry point: create one search from a natural-language request.

    async for ev in create_search(query, provider="openai", apps=["SF", "AOE"], token=lna):
        ...

Opens the MCP connection, selects the tool bundles, runs the chosen loop, and always
finishes with `usage`, `metrics` and `done` (or `agenterror`). The `done` message is the
CreatedSearch dict, which is the only thing the UI needs to load the search.
"""

import json
import logging
import uuid
from typing import AsyncIterator, Iterable

from create_search import config
from create_search.core.events import Event, event
from create_search.core.executor import ToolExecutor
from create_search.core.prompt import build_instructions
from create_search.core.recorder import RunRecorder
from create_search.core.stop import clear_stop
from create_search.core.toolset import drift, normalize_apps, select_tools
from create_search.loops import claude_loop, openai_loop
from create_search.mcp_connection import list_tool_dicts, open_mcp

logger = logging.getLogger(__name__)

LOOPS = {"openai": openai_loop.run_loop, "claude": claude_loop.run_loop}


def _check_query(query: str) -> str | None:
    """Input triage before any model call: reject empty or meaningless requests."""
    text = (query or "").strip()
    if not text:
        return "The request is empty. Describe the documents you want to find."
    if not any(ch.isalpha() for ch in text):
        return "The request has no words in it. Describe the documents you want to find."
    return None


async def create_search(
    query: str,
    *,
    provider: str = "openai",
    apps: Iterable[str] | None = None,
    token: str = "",
    model: str | None = None,
    effort: str = "medium",
    conversation_id: str | None = None,
    stop_key=None,
) -> AsyncIterator[Event]:
    conversation_id = conversation_id or uuid.uuid4().hex
    loop = LOOPS.get(provider)
    if loop is None:
        yield event("agenterror", f"Unknown provider '{provider}'. Use one of: {', '.join(LOOPS)}.")
        return
    problem = _check_query(query)
    if problem:
        yield event("agenterror", problem)
        return
    try:
        selected_apps = normalize_apps(apps)
    except ValueError as exc:
        yield event("agenterror", str(exc))
        return

    token = token or config.dev_token()
    recorder = RunRecorder(provider, model or "", query, selected_apps)
    pending: list[Event] = []

    def drain() -> list[Event]:
        out = list(pending)
        pending.clear()
        return out

    result = None
    try:
        async with open_mcp(token, conversation_id) as server:
            available = await list_tool_dicts(server)
            for problem in drift(available):
                yield event("warning", f"Tool classification: {problem}")
            toolset = select_tools(available, selected_apps)
            yield event(
                "status",
                f"{len(toolset.tools)} tools from {', '.join(selected_apps)} + shared lookups",
            )

            executor = ToolExecutor(server, toolset, pending.append, recorder)
            stopped = False
            async for ev in loop(
                executor=executor,
                instructions=build_instructions(selected_apps),
                query=query,
                model=model,
                effort=effort,
                recorder=recorder,
                drain=drain,
                stop_key=stop_key,
                conversation_id=conversation_id,
            ):
                yield ev
                if ev["kind"] == "stop":
                    stopped = True
                    break
            for ev in drain():
                yield ev

            result = executor.result(
                "stopped on request" if stopped else "run limit reached without a submitted search"
            )
    except Exception as exc:
        logger.exception("create_search failed: %s", conversation_id)
        error = f"{type(exc).__name__}: {exc}"
    else:
        error = None
    finally:
        clear_stop(stop_key)
        # Never raises; a lost capture must not cost the result.
        recorder.write(result.to_dict() if result else None)

    if error:
        yield event("agenterror", error)
        return
    if recorder.write_error:
        yield event("warning", f"Run not captured: {recorder.write_error}")

    totals = recorder.totals()
    usage = {k: totals[k] for k in ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens")}
    usage["requests"] = totals["model_calls"]
    yield event("usage", json.dumps(usage))
    yield event("metrics", totals)
    yield event("done", result.to_dict(), duration=totals["total_ms"] / 1000)
