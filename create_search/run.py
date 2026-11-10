"""Entry point: create one search from a natural-language request.

Always ends with usage, metrics and done (CreatedSearch dict), or agenterror.
"""

import json
import logging
import uuid
from typing import AsyncIterator, Iterable

from create_search import config
from create_search.clients import list_tool_dicts, open_mcp
from create_search.executor import ToolExecutor
from create_search.loops import claude_loop, openai_loop
from create_search.toolset import drift, normalize_apps, select_tools
from create_search.utils import Event, RunRecorder, build_instructions, clear_stop, event

logger = logging.getLogger(__name__)

LOOPS = {"openai": openai_loop.run_loop, "claude": claude_loop.run_loop}


def _check_query(query: str) -> str | None:
    text = (query or "").strip()
    if not text:
        return "The request is empty. Describe the documents you want to find."
    if not any(ch.isalpha() for ch in text):
        return "The request has no words in it. Describe the documents you want to find."
    return None


async def create_search(query: str, *, provider: str = "openai", apps: Iterable[str] | None = None, token: str = "",
                        model: str | None = None, effort: str = "medium", conversation_id: str | None = None,
                        stop_key=None) -> AsyncIterator[Event]:
    conversation_id = conversation_id or uuid.uuid4().hex
    if (loop := LOOPS.get(provider)) is None:
        yield event("agenterror", f"Unknown provider '{provider}'. Use one of: {', '.join(LOOPS)}.")
        return
    if problem := _check_query(query):
        yield event("agenterror", problem)
        return
    try:
        selected_apps = normalize_apps(apps)
    except ValueError as exc:
        yield event("agenterror", str(exc))
        return

    recorder = RunRecorder(provider, model or "", query, selected_apps)
    pending: list[Event] = []

    def drain() -> list[Event]:
        out = list(pending)
        pending.clear()
        return out

    result, error = None, None
    try:
        async with open_mcp(token or config.dev_token(), conversation_id) as server:
            available = await list_tool_dicts(server)
            for problem in drift(available):  # internal only: server log, not the UI stream
                logger.warning("Tool classification: %s", problem)
            toolset = select_tools(available, selected_apps)
            yield event("status", f"{len(toolset.tools)} tools from {', '.join(selected_apps)} + shared lookups")

            executor = ToolExecutor(server, toolset, pending.append, recorder)
            stopped = False
            async for ev in loop(executor=executor, instructions=build_instructions(selected_apps), query=query, model=model,
                                 effort=effort, recorder=recorder, drain=drain, stop_key=stop_key, conversation_id=conversation_id):
                yield ev
                if ev["kind"] == "stop":
                    stopped = True
                    break
            for ev in drain():
                yield ev
            result = executor.result("stopped on request" if stopped else "run limit reached without a submitted search")
    except Exception as exc:
        logger.exception("create_search failed: %s", conversation_id)
        error = f"{type(exc).__name__}: {exc}"
    finally:
        clear_stop(stop_key)
        recorder.write(result.to_dict() if result else None)  # never raises

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
