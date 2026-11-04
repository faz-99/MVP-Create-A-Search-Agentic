"""Claude loop: our own tool loop on the Anthropic SDK (Bedrock by default).

Kept separate from the Agents SDK for now; it is meant to be removable later without
touching core/. Differences from agentv4's BedrockAnthropicModel adapter, on purpose:

  - thinking blocks (with their signatures) are passed back unchanged in the assistant
    turn, which Claude needs to continue its reasoning across tool calls;
  - both the system prompt and the tool list carry cache_control, so the large, fixed
    tool schemas are billed at the cached rate after the first turn;
  - cache reads and writes are added to the input token count, since the API reports
    them separately from `input_tokens`.
"""

import asyncio
import time
from typing import Any, AsyncIterator, Callable

from create_search import config
from create_search.core.events import Event, event
from create_search.core.executor import ToolExecutor
from create_search.core.limits import submit_reminder, turn_notice
from create_search.core.recorder import RunRecorder
from create_search.core.stop import is_stop_requested
from create_search.models import get_claude_client

EFFORT_LEVELS = {"none": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max"}


def _tools(executor: ToolExecutor) -> list[dict[str, Any]]:
    tools = [
        {"name": d["name"], "description": d["description"], "input_schema": d["inputSchema"]}
        for d in executor.tool_defs()
    ]
    # One breakpoint after the last tool caches the whole (stable) tool list.
    tools[-1]["cache_control"] = {"type": "ephemeral"}
    return tools


def _thinking_kwargs(effort: str) -> dict[str, Any]:
    if config.CLAUDE_THINKING == "adaptive":
        # "summarized" so the UI has reasoning to show; the default returns it empty.
        return {
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": EFFORT_LEVELS.get(effort.lower(), "medium")},
        }
    if config.CLAUDE_THINKING == "enabled":
        return {"thinking": {"type": "enabled", "budget_tokens": config.CLAUDE_THINKING_BUDGET}}
    return {}


def _block_dict(block: Any) -> dict[str, Any] | None:
    """Assistant content block -> request dict, keeping only fields the API accepts."""
    kind = getattr(block, "type", "")
    if kind == "text":
        return {"type": "text", "text": block.text} if block.text else None
    if kind == "thinking":
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    if kind == "redacted_thinking":
        return {"type": "redacted_thinking", "data": block.data}
    if kind == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return None


async def run_loop(
    *,
    executor: ToolExecutor,
    instructions: str,
    query: str,
    model: str | None,
    effort: str,
    recorder: RunRecorder,
    drain: Callable[[], list[Event]],
    stop_key,
    conversation_id: str,
) -> AsyncIterator[Event]:
    client = get_claude_client()
    model_name = model or config.DEFAULT_CLAUDE_MODEL
    tools = _tools(executor)
    system = [{"type": "text", "text": instructions, "cache_control": {"type": "ephemeral"}}]
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"type": "text", "text": query}]}]
    reminders = 0

    for turn in range(1, config.MAX_TURNS + 1):
        if is_stop_requested(stop_key):
            yield event("stop")
            return

        started = time.perf_counter()
        response = await client.messages.create(
            model=model_name,
            max_tokens=config.CLAUDE_MAX_TOKENS,
            system=system,
            tools=tools,
            messages=messages,
            extra_headers={"X-Conversation": conversation_id},
            **_thinking_kwargs(effort),
        )
        usage = response.usage
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        yield event(
            "step",
            recorder.step(
                "model",
                model_name,
                time.perf_counter() - started,
                input_tokens=usage.input_tokens + cache_read + cache_write,
                cached_tokens=cache_read,
                output_tokens=usage.output_tokens,
                detail=response.stop_reason or "",
            ),
        )

        content = [b for b in (_block_dict(block) for block in response.content) if b]
        for block in content:
            if block["type"] == "thinking" and block["thinking"]:
                yield event("reasoning", block["thinking"])
        tool_uses = [b for b in content if b["type"] == "tool_use"]

        if response.stop_reason == "refusal":
            # A safety decline; reminding would only repeat it. The run falls back.
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) or "unspecified"
            yield event("warning", f"The model declined this request (category: {category}).")
            return

        if response.stop_reason == "max_tokens":
            # A cut-off tool call may carry partial input; drop it and ask again.
            content = [b for b in content if b["type"] != "tool_use"]
            tool_uses = []
            yield event("warning", "The model hit its output limit; asking it to continue.")

        if content:
            messages.append({"role": "assistant", "content": content})

        if not tool_uses:
            if reminders >= config.MAX_SUBMIT_REMINDERS:
                yield event("warning", "The model kept replying without submitting a search.")
                return
            reminders += 1
            yield event("status", f"No search submitted; reminding the model ({reminders}/{config.MAX_SUBMIT_REMINDERS}).")
            messages.append({"role": "user", "content": [{"type": "text", "text": submit_reminder()}]})
            continue

        outcomes = await asyncio.gather(
            *(executor.execute(b["name"], b["input"] or {}, call_id=b["id"]) for b in tool_uses)
        )
        for pending in drain():
            yield pending
        if executor.submitted is not None:
            return

        results: list[dict[str, Any]] = [
            {"type": "tool_result", "tool_use_id": b["id"], "content": text, "is_error": is_error}
            for b, (text, is_error) in zip(tool_uses, outcomes)
        ]
        notice = turn_notice(turn, config.MAX_TURNS)
        if notice:
            results.append({"type": "text", "text": notice})
        messages.append({"role": "user", "content": results})

    yield event("warning", f"Stopped after {config.MAX_TURNS} turns.")
