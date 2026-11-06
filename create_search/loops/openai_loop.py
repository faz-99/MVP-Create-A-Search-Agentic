"""OpenAI loop on the Agents SDK (as agentv4).

MCP tools are wrapped as FunctionTools that call ToolExecutor, so both loops share one policy.
The run ends on an accepted submit_search; a reply without it gets up to MAX_SUBMIT_REMINDERS reminders.
"""

import json
import time
from typing import Any, AsyncIterator, Callable

from agents import (Agent, FunctionTool, MaxTurnsExceeded, ModelSettings, RunConfig, RunContextWrapper, RunHooks, Runner,
                    ToolsToFinalOutputResult, set_tracing_disabled)
from agents.items import ModelResponse
from agents.tool_context import ToolContext
from openai.types.shared import Reasoning

from create_search import config
from create_search.clients import get_openai_model, openai_config
from create_search.executor import ToolExecutor
from create_search.utils import Event, RunRecorder, event, is_stop_requested, submit_reminder, turn_notice

set_tracing_disabled(True)


def _function_tool(definition: dict[str, Any], executor: ToolExecutor) -> FunctionTool:
    name = definition["name"]

    async def invoke(ctx: ToolContext, args_json: str) -> str:
        try:
            args = json.loads(args_json or "{}")
        except json.JSONDecodeError as exc:
            return f"Arguments were not valid JSON: {exc}"
        text, _ = await executor.execute(name, args if isinstance(args, dict) else {}, call_id=ctx.tool_call_id)
        return text

    # strict off: MCP schemas use anyOf/null defaults strict mode rejects; the executor validates instead.
    return FunctionTool(name=name, description=definition["description"], params_json_schema=definition["inputSchema"],
                        on_invoke_tool=invoke, strict_json_schema=False)


class _LoopHooks(RunHooks):
    """Times each model call, records usage, adds the wrap-up notice."""

    def __init__(self, recorder: RunRecorder, emit: Callable[[Event], None], model: str):
        self.recorder, self.emit, self.model = recorder, emit, model
        self.turn = 0
        self._started = 0.0

    async def on_llm_start(self, context: RunContextWrapper, agent: Agent, system_prompt, input_items) -> None:
        self.turn += 1
        self._started = time.perf_counter()
        if notice := turn_notice(self.turn, config.MAX_TURNS):
            input_items.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": notice}]})

    async def on_llm_end(self, context: RunContextWrapper, agent: Agent, response: ModelResponse) -> None:
        usage = response.usage
        cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0
        reasoning = getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0) or 0
        self.emit(event("step", self.recorder.step("model", self.model, time.perf_counter() - self._started, input_tokens=usage.input_tokens,
                                                   cached_tokens=cached, output_tokens=usage.output_tokens, reasoning_tokens=reasoning)))


async def run_loop(*, executor: ToolExecutor, instructions: str, query: str, model: str | None, effort: str, recorder: RunRecorder,
                   drain: Callable[[], list[Event]], stop_key, conversation_id: str) -> AsyncIterator[Event]:
    cfg = openai_config(model)
    model_name = cfg["model"]
    reasoning = None if "gpt-4" in model_name else Reasoning(effort=effort, summary="detailed")
    settings = ModelSettings(tool_choice="auto", parallel_tool_calls=True, truncation="disabled", store=False, reasoning=reasoning,
                             response_include=["reasoning.encrypted_content"] if reasoning else [],  # store=False: reasoning travels back encrypted
                             extra_headers={"X-Conversation": conversation_id})

    def stop_when_submitted(context: RunContextWrapper, results) -> ToolsToFinalOutputResult:
        done = executor.submitted is not None
        return ToolsToFinalOutputResult(is_final_output=done, final_output="submitted" if done else None)

    agent = Agent(name="CreateSearch", instructions=instructions, model=get_openai_model(cfg["apikey"], cfg["baseurl"], model_name),
                  tools=[_function_tool(d, executor) for d in executor.tool_defs()], model_settings=settings,
                  tool_use_behavior=stop_when_submitted, reset_tool_choice=True)

    emitted: list[Event] = []
    hooks = _LoopHooks(recorder, emitted.append, model_name)
    run_input: Any = [{"role": "user", "content": [{"type": "input_text", "text": query}]}]
    reminders = 0

    def flush() -> list[Event]:
        out = drain() + emitted
        emitted.clear()
        return out

    while True:
        turns_left = config.MAX_TURNS - hooks.turn
        if turns_left <= 0:
            yield event("warning", f"Stopped after {config.MAX_TURNS} turns.")
            return
        result = Runner.run_streamed(agent, run_input, run_config=RunConfig(), max_turns=turns_left, hooks=hooks)
        last_stop_check = 0.0
        try:
            async for ev in result.stream_events():
                for pending in flush():
                    yield pending
                if (now := time.monotonic()) - last_stop_check >= 0.5:
                    last_stop_check = now
                    if is_stop_requested(stop_key):
                        result.cancel()
                        yield event("stop")
                        return
                if ev.type == "raw_response_event" and getattr(ev.data, "type", "") == "response.reasoning_summary_part.done":
                    yield event("reasoning", ev.data.part.text)
        except MaxTurnsExceeded:
            for pending in flush():
                yield pending
            yield event("warning", f"Stopped after {config.MAX_TURNS} turns.")
            return

        for pending in flush():
            yield pending
        if executor.submitted is not None:
            return
        if reminders >= config.MAX_SUBMIT_REMINDERS:
            yield event("warning", "The model kept replying without submitting a search.")
            return
        reminders += 1
        yield event("status", f"No search submitted; reminding the model ({reminders}/{config.MAX_SUBMIT_REMINDERS}).")
        run_input = result.to_input_list() + [{"role": "user", "content": [{"type": "input_text", "text": submit_reminder()}]}]
