"""Runs every tool call for both loops, with the same policy.

This is where Create Search differs from a general research agent:

  - search tools may run, but only MAX_SEARCH_RUNS times, with pageSize clamped to
    SEARCH_PREVIEW_ROWS and the result summarised. The agent runs searches to check
    its filters, not to read documents — the UI re-runs the final payload itself.
  - an identical repeated call is answered from the earlier result, not re-run.
  - a 403 is reported as an entitlement problem the agent must not retry.
  - `submit_search` validates the payload against the live schema; errors go back to
    the model as the tool result, so repair is simply its next turn.
  - if limits end the run before a submit, `fallback()` offers the last search the agent
    ran successfully, marked as such.

Tool results are returned as (text, is_error) and never raise: a failed tool is
something for the model to react to, not a reason to fail the run.
"""

import json
import time
from typing import Any

from agents.mcp import MCPServer

from create_search import config
from create_search.core.contract import (
    STRATEGY_TOOL,
    SUBMIT_TOOL,
    CreatedSearch,
    strategy_tool_def,
    submit_tool_def,
)
from create_search.core.events import Emit, event
from create_search.core.recorder import RunRecorder
from create_search.core.toolset import Toolset, is_search_tool
from create_search.core.trimming import summarize_search, truncate
from create_search.core.validate import validate_payload


def _key(name: str, args: dict[str, Any]) -> str:
    return name + ":" + json.dumps(args, sort_keys=True, default=str)


def _result_text(result: Any) -> str:
    parts = [
        getattr(block, "text", "")
        for block in (getattr(result, "content", None) or [])
        if getattr(block, "type", "") == "text"
    ]
    text = "\n".join(p for p in parts if p)
    if text:
        return text
    structured = getattr(result, "structuredContent", None)
    return json.dumps(structured, default=str) if structured is not None else ""


class ToolExecutor:
    def __init__(self, server: MCPServer, toolset: Toolset, emit: Emit, recorder: RunRecorder):
        self.server = server
        self.toolset = toolset
        self.emit = emit
        self.recorder = recorder
        self.strategy = ""
        self.submitted: CreatedSearch | None = None
        self.last_valid_run: tuple[str, dict[str, Any]] | None = None
        self.search_runs = 0
        self.tool_calls = 0
        self._seen: dict[str, str] = {}

    # -- what the model sees ----------------------------------------------------

    def tool_defs(self) -> list[dict[str, Any]]:
        """MCP tools for this request, then the two agent tools. Order is stable so the
        prompt prefix (and its cache) is identical across turns."""
        defs = [
            {
                "name": t["name"],
                "description": t.get("description") or t["name"],
                "inputSchema": t.get("inputSchema") or {"type": "object", "properties": {}},
            }
            for _, t in sorted(self.toolset.tools.items())
        ]
        defs.append(strategy_tool_def())
        defs.append(submit_tool_def(self.toolset.search_tools))
        return defs

    # -- execution ----------------------------------------------------------------

    async def execute(self, name: str, args: dict[str, Any], call_id: str = "") -> tuple[str, bool]:
        started = time.perf_counter()
        self.tool_calls += 1
        self.emit(event("trace", f"Tool call: {name} {json.dumps(args, default=str)}", call_id=call_id))

        full_text: str | None = None
        if name == STRATEGY_TOOL:
            text, is_error = self._report_strategy(args)
        elif name == SUBMIT_TOOL:
            text, is_error = self._submit(args)
        elif name not in self.toolset.tools:
            text, is_error = f"'{name}' is not available for this request.", True
        elif self.tool_calls > config.MAX_TOOL_CALLS:
            text, is_error = (
                f"Tool budget used ({config.MAX_TOOL_CALLS} calls). Call {SUBMIT_TOOL} now.",
                True,
            )
        else:
            text, is_error, full_text = await self._call_mcp(name, args)

        seconds = time.perf_counter() - started
        self.emit(event("step", self.recorder.step("tool", name, seconds, detail="error" if is_error else "ok")))
        self.recorder.call(call_id, name, args, full_text if full_text is not None else text, is_error)
        self.emit(
            event("trace", f"Tool output {name}: {text[:500]}", call_id=call_id, duration=seconds, ok=not is_error)
        )
        return text, is_error

    async def _call_mcp(self, name: str, args: dict[str, Any]) -> tuple[str, bool, str | None]:
        searching = is_search_tool(name)
        key = _key(name, args)
        if key in self._seen:
            return (
                "Identical to an earlier call; same result as before. Change something "
                "specific or move on.\n" + self._seen[key],
                False,
                None,
            )

        if searching and self.search_runs >= config.MAX_SEARCH_RUNS:
            return (
                f"Search budget used ({config.MAX_SEARCH_RUNS} runs). Do not run more "
                f"searches; call {SUBMIT_TOOL} with your best valid payload.",
                True,
                None,
            )
        errors = validate_payload(name, args, self.toolset.schema(name))
        if errors:
            return "Arguments rejected before running:\n- " + "\n- ".join(errors), True, None
        if searching:
            self.search_runs += 1

        sent = dict(args)
        schema_props = (self.toolset.schema(name) or {}).get("properties", {})
        if searching and "pageSize" in schema_props:
            sent["pageSize"] = min(int(sent.get("pageSize") or config.SEARCH_PREVIEW_ROWS), config.SEARCH_PREVIEW_ROWS)

        try:
            result = await self.server.call_tool(name, sent)
        except Exception as exc:  # transport failure after the SDK's own retries
            return f"Tool call failed: {type(exc).__name__}: {exc}", True, None

        full = _result_text(result)
        is_error = bool(getattr(result, "isError", False))
        if is_error:
            if "403" in full or "Forbidden" in full:
                text = (
                    f"403 Forbidden: this user is not entitled to the dataset behind "
                    f"'{name}'. Do not retry it; use another dataset or record it in "
                    f"unresolved_conditions. Server said: {full[:200]}"
                )
            else:
                text = truncate(full, 1500) or "Tool reported an error with no message."
            return text, True, full

        if searching:
            self.last_valid_run = (name, args)
            text = (
                f"Search ran (run {self.search_runs} of {config.MAX_SEARCH_RUNS}; preview "
                f"of first {config.SEARCH_PREVIEW_ROWS} rows only):\n"
                + summarize_search(full, config.SEARCH_PREVIEW_ROWS)
            )
        else:
            text = truncate(full, config.MAX_LOOKUP_CHARS)
        self._seen[key] = text
        return text, False, full

    def _report_strategy(self, args: dict[str, Any]) -> tuple[str, bool]:
        strategy = str(args.get("strategy") or "").strip()
        if not strategy:
            return "strategy is empty.", True
        self.strategy = strategy
        self.emit(event("strategy", strategy, final=False))
        return "Strategy shown to the user. Continue building the search.", False

    def _submit(self, args: dict[str, Any]) -> tuple[str, bool]:
        tool = str(args.get("tool") or "")
        payload = args.get("payload")
        if tool not in self.toolset.search_tools:
            errors = [f"'{tool}' is not one of: {', '.join(self.toolset.search_tools)}"]
        else:
            errors = validate_payload(tool, payload, self.toolset.schema(tool))
        if errors:
            self.emit(event("warning", "Submitted search rejected: " + "; ".join(errors[:3])))
            return (
                "Rejected. Fix these and call submit_search again:\n- " + "\n- ".join(errors),
                True,
            )

        strategy = str(args.get("strategy") or self.strategy)
        self.submitted = CreatedSearch(
            strategy=strategy,
            tool=tool,
            payload=payload,
            analysis_instructions=str(args.get("analysis_instructions") or ""),
            assumptions=[str(a) for a in args.get("assumptions") or []],
            unresolved_conditions=[str(u) for u in args.get("unresolved_conditions") or []],
            notes=str(args.get("notes") or ""),
            source="agent",
        )
        self.emit(event("strategy", strategy, final=True))
        return "Accepted. The search is final. Reply with one short line and stop.", False

    # -- end of run ---------------------------------------------------------------

    def result(self, reason: str) -> CreatedSearch:
        """The submitted search, else the last valid search run, else an empty result."""
        if self.submitted is not None:
            return self.submitted
        if self.last_valid_run is not None:
            tool, payload = self.last_valid_run
            return CreatedSearch(
                strategy=self.strategy,
                tool=tool,
                payload=payload,
                unresolved_conditions=[
                    f"The agent stopped before finalizing ({reason}). This is the last "
                    "search it ran successfully; review it before relying on it."
                ],
                source="fallback",
            )
        return CreatedSearch(
            strategy=self.strategy,
            unresolved_conditions=[f"No search was created ({reason})."],
        )
