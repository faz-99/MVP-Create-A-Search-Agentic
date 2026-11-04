"""OpenAI loop: the Agents SDK, set up as agentv4 does.

The MCP tools are not attached as an `mcp_servers` entry. They are wrapped as
FunctionTools that call ToolExecutor, so this loop and the Claude loop apply exactly the
same search budget, result trimming and submit validation.

The run ends when `submit_search` is accepted (custom tool_use_behavior). A reply that
ends without it gets up to MAX_SUBMIT_REMINDERS reminders; the turn limit and the
wrap-up notice come from config/limits, as in agentv4's ContextSizeHooks.
"""

import json
import time
from typing import Any, AsyncIterator, Callable

from agents import (
    Agent,
    FunctionTool,
    MaxTurnsExceeded,
    ModelSettings,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    Runner,
    ToolsToFinalOutputResult,
    set_tracing_disabled,
)
from agents.items import ModelResponse
from agents.tool_context import ToolContext
from openai.types.shared import Reasoning

from create_search import config
from create_search.core.events import Event, event
from create_search.core.executor import ToolExecutor
from create_search.core.limits import submit_reminder, turn_notice
from create_search.core.recorder import RunRecorder
from create_search.core.stop import is_stop_requested
from create_search.models import get_openai_model, openai_config

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

    return FunctionTool(
        name=name,
        description=definition["description"],
        params_json_schema=definition["inputSchema"],
        on_invoke_tool=invoke,
        # MCP schemas use anyOf/null defaults that strict mode rejects; the executor
        # validates search arguments itself.
        strict_json_schema=False,
    )


class _LoopHooks(RunHooks):
    """Times each model call, records its usage, and adds the wrap-up notice."""

    def __init__(self, recorder: RunRecorder, emit: Callable[[Event], None], model: str):
        self.recorder = recorder
        self.emit = emit
        self.model = model
        self.turn = 0
        self._started = 0.0

    async def on_llm_start(self, context: RunContextWrapper, agent: Agent, system_prompt, input_items) -> None:
        self.turn += 1
        self._started = time.perf_counter()
        notice = turn_notice(self.turn, config.MAX_TURNS)
        if notice:
            input_items.append(
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": notice}]}
            )

    async def on_llm_end(self, context: RunContextWrapper, agent: Agent, response: ModelResponse) -> None:
        usage = response.usage
        cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0
        reasoning = getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0) or 0
        step = self.recorder.step(
            "model",
            self.model,
            time.perf_counter() - self._started,
            input_tokens=usage.input_tokens,
            cached_tokens=cached,
            output_tokens=usage.output_tokens,
            reasoning_tokens=reasoning,
        )
        self.emit(event("step", step))


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
    cfg = openai_config(model)
    model_name = cfg["model"]
    reasoning = None if "gpt-4" in model_name else Reasoning(effort=effort, summary="detailed")
    settings = ModelSettings(
        tool_choice="auto",
        parallel_tool_calls=True,
        truncation="disabled",
        store=False,
        reasoning=reasoning,
        # store=False is stateless, so reasoning items must travel back encrypted.
        response_include=["reasoning.encrypted_content"] if reasoning else [],
        extra_headers={"X-Conversation": conversation_id},
    )

    def stop_when_submitted(context: RunContextWrapper, results) -> ToolsToFinalOutputResult:
        if executor.submitted is not None:
            return ToolsToFinalOutputResult(is_final_output=True, final_output="submitted")
        return ToolsToFinalOutputResult(is_final_output=False, final_output=None)

    agent = Agent(
        name="CreateSearch",
        instructions=instructions,
        model=get_openai_model(cfg["apikey"], cfg["baseurl"], model_name),
        tools=[_function_tool(d, executor) for d in executor.tool_defs()],
        model_settings=settings,
        tool_use_behavior=stop_when_submitted,
        reset_tool_choice=True,
    )

    emitted: list[Event] = []
    hooks = _LoopHooks(recorder, emitted.append, model_name)
    run_input: Any = [{"role": "user", "content": [{"type": "input_text", "text": query}]}]
    reminders = 0

    while True:
        turns_left = config.MAX_TURNS - hooks.turn
        if turns_left <= 0:
            yield event("warning", f"Stopped after {config.MAX_TURNS} turns.")
            return
        result = Runner.run_streamed(
            agent, run_input, run_config=RunConfig(), max_turns=turns_left, hooks=hooks
        )
        last_stop_check = 0.0
        try:
            async for ev in result.stream_events():
                for pending in drain() + emitted:
                    yield pending
                emitted.clear()

                now = time.monotonic()
                if now - last_stop_check >= 0.5:
                    last_stop_check = now
                    if is_stop_requested(stop_key):
                        result.cancel()
                        yield event("stop")
                        return

                if ev.type == "raw_response_event":
                    data = ev.data
                    if getattr(data, "type", "") == "response.reasoning_summary_part.done":
                        yield event("reasoning", data.part.text)
        except MaxTurnsExceeded:
            for pending in drain() + emitted:
                yield pending
            emitted.clear()
            yield event("warning", f"Stopped after {config.MAX_TURNS} turns.")
            return

        for pending in drain() + emitted:
            yield pending
        emitted.clear()

        if executor.submitted is not None:
            return
        if reminders >= config.MAX_SUBMIT_REMINDERS:
            yield event("warning", "The model kept replying without submitting a search.")
            return
        reminders += 1
        yield event("status", f"No search submitted; reminding the model ({reminders}/{config.MAX_SUBMIT_REMINDERS}).")
        run_input = result.to_input_list() + [
            {"role": "user", "content": [{"type": "input_text", "text": submit_reminder()}]}
        ]
