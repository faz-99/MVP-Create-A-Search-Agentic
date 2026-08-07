"""The client-side tool loop — phase 1.

Runs the tool loop here rather than letting the provider do it. That is forced, not
chosen: the MCP server authenticates via a custom `lna` header, and no provider's
hosted MCP connector can send an arbitrary header (Anthropic takes
`authorization_token`, OpenAI takes `authorization`; both emit only Authorization).
Bedrock does not support the MCP connector at all.

It turns out better this way — the loop is identical for every provider, so the two
agents differ only in the `LLM` adapter behind them, which is what makes comparing
them meaningful.

Two invariants:

  - The search executors are never registered, so the model cannot run a search.
    That is the gate. It can still *read* their schemas via DESCRIBE_TOOL, because
    building valid arguments without the schema is impossible — the first run of this
    agent invented three field names when the schema was hidden.
  - A created search is validated against the real schema before it is returned, and
    the model gets up to MAX_REPAIRS attempts to fix its own errors. Emitting an
    invalid payload for a human to discover at Run time is a worse failure than
    spending another turn here.
"""

import json
import time
from typing import Any, Callable

from shared.capture import RunCapture
from shared.llm import LLM, Completion
from shared.mcp_client import MCPClient
from shared.plan import SearchPlan, parse_plan
from shared.prompts import BUILD_SYSTEM_PROMPT
from shared.toolsets import is_execute_tool, unclassified_search_tools
from shared.validate import validate_payload

MAX_ITERATIONS = 12
MAX_REPAIRS = 2

DESCRIBE_TOOL = "describe_search_tool"

# A reply cut off at the token limit loses its trailing JSON block — the one part that
# is the deliverable. Bedrock says "max_tokens", Chat Completions says "length".
TRUNCATED_STOP_REASONS = {"max_tokens", "length"}

# Emitted for the UI: {"type": "thinking"|"text"|"tool"|"tool_result"|"status", ...}
EventSink = Callable[[dict[str, Any]], None]


def _noop(_event: dict[str, Any]) -> None:
    pass


def _describe_tool_def(execute_names: list[str]) -> dict[str, Any]:
    """A synthetic, read-only tool exposing the withheld executors' schemas.

    Withholding the executors stops the model running a search, but it also hides
    their input schemas — and the model's whole job is to build arguments for one of
    them. Reading a schema is not executing a search, so this closes the gap without
    weakening the gate.
    """
    return {
        "name": DESCRIBE_TOOL,
        "description": (
            "Return the full input schema for one of the search tools. Call this "
            "before building arguments so field names and types come from the schema "
            "rather than from guesswork. Available: " + ", ".join(execute_names)
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "enum": execute_names,
                    "description": "Which search tool to describe.",
                }
            },
            "required": ["tool_name"],
        },
    }


def build_phase_tools(available: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build-phase tool set: everything except the executors, plus the describer."""
    execute_names = sorted(
        t["name"] for t in available if is_execute_tool(t.get("name", ""))
    )
    tools = [t for t in available if not is_execute_tool(t.get("name", ""))]
    tools.append(_describe_tool_def(execute_names))
    return tools


def _describe(available: list[dict[str, Any]], tool_name: str) -> tuple[str, bool]:
    """Serve the synthetic describe call from the live tool list."""
    for tool in available:
        if tool.get("name") == tool_name:
            return (
                json.dumps(
                    {
                        "name": tool.get("name"),
                        "description": tool.get("description"),
                        "inputSchema": tool.get("inputSchema"),
                    },
                    indent=2,
                ),
                False,
            )
    known = ", ".join(
        sorted(t["name"] for t in available if is_execute_tool(t.get("name", "")))
    )
    return (f"No search tool named '{tool_name}'. Available: {known}", True)


def result_to_text(result: dict[str, Any]) -> str:
    """Flatten an MCP tool result into text for the model.

    Tool results go back as text on every provider, so the shape normalising belongs
    here, not in the adapters.
    """
    parts = [
        block.get("text", "")
        for block in result.get("content", [])
        if block.get("type") == "text"
    ]
    text = "\n".join(p for p in parts if p)
    return text or json.dumps(result, default=repr)


def _repair_request(tool: str, errors: list[str]) -> dict[str, Any]:
    body = (
        f"The search you emitted does not match the schema for '{tool}':\n\n- "
        + "\n- ".join(errors)
        + "\n\nCall the describe tool for that tool if you need the schema again, "
        "then emit a corrected JSON block. Use only field names the schema defines."
    )
    return {"role": "user", "content": [{"type": "text", "text": body}]}


def _plan_request(reason: str | None) -> dict[str, Any]:
    body = (
        "Your reply did not end with the required search block"
        + (f" ({reason})" if reason else "")
        + ".\n\nEmit it now and nothing else: one ```json fenced object with `tool`, "
        "`payload` and `analysis_instructions`. Do not restate your reasoning — the "
        "work is done, only the block is missing."
    )
    return {"role": "user", "content": [{"type": "text", "text": body}]}


def run_build(
    llm: LLM,
    query: str,
    on_event: EventSink | None = None,
    max_iterations: int = MAX_ITERATIONS,
) -> SearchPlan:
    """Drive the build phase to a validated SearchPlan.

    One MCP session for the whole loop, so resolver calls share it.
    """
    emit = on_event or _noop
    capture = RunCapture(llm.name.split(":")[0], llm.name, query)
    transcript: list[str] = []
    repairs = 0
    available: list[dict[str, Any]] = []

    # Per-step timing and tokens. Kept here rather than in the adapters so both
    # providers are measured the same way — otherwise the numbers aren't comparable,
    # which is the whole point of running two agents.
    steps: list[dict[str, Any]] = []
    started = time.perf_counter()

    def record(kind: str, label: str, elapsed: float, **extra: Any) -> None:
        step = {
            "step": len(steps) + 1,
            "kind": kind,
            "label": label,
            "ms": round(elapsed * 1000),
            **extra,
        }
        steps.append(step)
        emit({"type": "step", **step})

    def totals() -> dict[str, Any]:
        return {
            "type": "metrics",
            "steps": steps,
            "total_ms": round((time.perf_counter() - started) * 1000),
            "model_ms": sum(s["ms"] for s in steps if s["kind"] == "model"),
            "tool_ms": sum(s["ms"] for s in steps if s["kind"] == "tool"),
            "input_tokens": sum(s.get("input_tokens", 0) for s in steps),
            "output_tokens": sum(s.get("output_tokens", 0) for s in steps),
            "model_calls": sum(1 for s in steps if s["kind"] == "model"),
            "tool_calls": sum(1 for s in steps if s["kind"] == "tool"),
        }

    try:
        with MCPClient() as mcp:
            t0 = time.perf_counter()
            available = mcp.list_tools()
            record("mcp", "tools/list", time.perf_counter() - t0,
                   detail=f"{len(available)} tools")
            tools = build_phase_tools(available)
            emit({"type": "status", "text": f"{len(tools)} tools available"})

            # A renamed executor would otherwise become silently callable here.
            drift = unclassified_search_tools(available)
            if drift:
                emit(
                    {
                        "type": "invalid",
                        "text": (
                            "GATE WARNING: unclassified search tool(s) "
                            f"{drift} — classify them in shared/toolsets.py. "
                            "Until then they are callable during the build phase."
                        ),
                    }
                )

            messages: list[dict[str, Any]] = [
                {"role": "user", "content": [{"type": "text", "text": query}]}
            ]

            for _ in range(max_iterations):
                t0 = time.perf_counter()
                completion: Completion = llm.complete(
                    BUILD_SYSTEM_PROMPT, messages, tools
                )
                record(
                    "model", llm.name, time.perf_counter() - t0,
                    input_tokens=completion.input_tokens,
                    output_tokens=completion.output_tokens,
                    detail=completion.stop_reason or "",
                )

                if completion.thinking:
                    emit({"type": "thinking", "text": completion.thinking})
                if completion.text:
                    transcript.append(completion.text)
                    emit({"type": "text", "text": completion.text})

                if not completion.wants_tools:
                    plan = parse_plan("\n".join(transcript))

                    if not (plan.tool and isinstance(plan.payload, dict)):
                        # The reply ended without the block. Ask for it rather than
                        # giving up — the tool work is already done, and throwing it
                        # away shows the operator "no runnable search" for what is
                        # usually one missing fence.
                        if repairs < MAX_REPAIRS:
                            repairs += 1
                            if completion.stop_reason in TRUNCATED_STOP_REASONS:
                                emit(
                                    {
                                        "type": "warning",
                                        "text": (
                                            "The model hit its output token limit, so "
                                            "the trailing JSON block was cut off."
                                        ),
                                    }
                                )
                            emit(
                                {
                                    "type": "status",
                                    "text": (
                                        "no search block — asking for it "
                                        f"({repairs}/{MAX_REPAIRS})"
                                    ),
                                }
                            )
                            # Only when there is text: Bedrock rejects an empty text
                            # block, and an empty reply is exactly one way to get here.
                            if completion.text:
                                messages.append(
                                    {
                                        "role": "assistant",
                                        "content": [
                                            {"type": "text", "text": completion.text}
                                        ],
                                    }
                                )
                            messages.append(_plan_request(plan.parse_error))
                            transcript.clear()
                            continue

                        # Still report timings: the runs that most need explaining
                        # were the only ones with no totals row in the UI.
                        emit(totals())
                        break  # nothing to validate — report as parsed

                    errors = validate_payload(plan.tool, plan.payload, available)
                    if not errors:
                        emit({"type": "status", "text": "payload validates"})
                        emit(totals())
                        return plan
                    if repairs >= MAX_REPAIRS:
                        emit({"type": "status", "text": "payload still invalid"})
                        plan.validation_errors = errors
                        emit(totals())
                        return plan

                    repairs += 1
                    emit(
                        {
                            "type": "status",
                            "text": f"payload invalid — repair {repairs}/{MAX_REPAIRS}",
                        }
                    )
                    for err in errors[:4]:
                        emit({"type": "invalid", "text": err})

                    # The corrected block must not be mixed with the rejected one,
                    # since parse_plan reads the last JSON block it finds.
                    messages.append(
                        {
                            "role": "assistant",
                            "content": [{"type": "text", "text": completion.text}],
                        }
                    )
                    messages.append(_repair_request(plan.tool, errors))
                    transcript.clear()
                    continue

                # Assistant turn: whatever it said, plus the calls it wants run.
                assistant: list[dict[str, Any]] = []
                if completion.text:
                    assistant.append({"type": "text", "text": completion.text})
                for call in completion.tool_calls:
                    assistant.append(
                        {
                            "type": "tool_use",
                            "id": call.id,
                            "name": call.name,
                            "input": call.arguments,
                        }
                    )
                messages.append({"role": "assistant", "content": assistant})

                results: list[dict[str, Any]] = []
                for call in completion.tool_calls:
                    emit({"type": "tool", "name": call.name, "input": call.arguments})
                    capture.record_call(call.name, call.arguments, call_id=call.id)

                    t0 = time.perf_counter()
                    if call.name == DESCRIBE_TOOL:
                        text, is_error = _describe(
                            available, str(call.arguments.get("tool_name", ""))
                        )
                    elif is_execute_tool(call.name):
                        # Unreachable via the registered set; refuse loudly if a
                        # provider ever surfaces one anyway.
                        text = (
                            f"'{call.name}' executes a search and is not available "
                            "during this phase. Emit the created search instead; a "
                            "human decides whether to run it."
                        )
                        is_error = True
                    else:
                        try:
                            raw = mcp.call_tool(call.name, call.arguments)
                            text = result_to_text(raw)
                            is_error = bool(raw.get("isError", False))
                        except Exception as exc:  # transport/protocol failure
                            text = f"{type(exc).__name__}: {exc}"
                            is_error = True

                    record(
                        "tool", call.name, time.perf_counter() - t0,
                        detail="ok" if not is_error else "error",
                    )
                    capture.record_result(text, call_id=call.id, error=is_error or None)
                    emit(
                        {
                            "type": "tool_result",
                            "name": call.name,
                            "ok": not is_error,
                            "preview": text[:200],
                        }
                    )
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call.id,
                            "content": text,
                            "is_error": is_error,
                        }
                    )

                messages.append({"role": "user", "content": results})
            else:
                emit(
                    {
                        "type": "status",
                        "text": f"stopped after {max_iterations} iterations",
                    }
                )
                emit(totals())
    finally:
        # An exception here would replace the plan returned above. capture.write()
        # no longer raises; this guard keeps that true if it regresses.
        try:
            if capture.write() is None and capture.write_error:
                emit({"type": "warning", "text": f"Run not captured: {capture.write_error}"})
        except Exception as exc:  # never let bookkeeping break the run
            emit({"type": "warning", "text": f"Run not captured: {exc!r}"})

    plan = parse_plan("\n".join(transcript))
    if plan.tool and isinstance(plan.payload, dict):
        plan.validation_errors = validate_payload(plan.tool, plan.payload, available)
    return plan
