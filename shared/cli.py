"""Terminal rendering for the build loop. Shared so both agents print identically —
a formatting difference between them would read as a behavioural difference.
"""

import json
import sys
from typing import Any

from shared.plan import SearchPlan

# Tool results carry arbitrary Unicode (➤, curly quotes, dashes) and the Windows
# console defaults to cp1252, which raises UnicodeEncodeError mid-print and kills the
# run. Replace unencodable characters instead of losing the whole result.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def show_event(event: dict[str, Any]) -> None:
    kind = event.get("type")
    if kind == "thinking":
        print(f"\n  [thinking] {event['text'][:200]}", flush=True)
    elif kind == "text":
        print(event["text"], end="", flush=True)
    elif kind == "tool":
        args = json.dumps(event.get("input") or {}, default=repr)[:120]
        print(f"\n  -> {event['name']}({args})", flush=True)
    elif kind == "tool_result":
        mark = "ok " if event.get("ok") else "ERR"
        print(f"\n     [{mark}] {(event.get('preview') or '')[:120]}", flush=True)
    elif kind == "invalid":
        print(f"\n     [schema] {event['text'][:200]}", flush=True)
    elif kind == "warning":
        # Non-fatal bookkeeping failure. "invalid" is about the payload; this is not.
        print(f"\n  [warning] {event['text']}", flush=True)
    elif kind == "status":
        print(f"\n  ({event['text']})", flush=True)
    elif kind == "step":
        tokens = ""
        if event.get("input_tokens") or event.get("output_tokens"):
            tokens = f"  in={event.get('input_tokens', 0)} out={event.get('output_tokens', 0)}"
        print(
            f"\n     [{event['ms']:>6} ms] {event['kind']}: {event['label']}{tokens}",
            flush=True,
        )
    elif kind == "metrics":
        print(
            f"\n\n--- timing ---\n"
            f"  total     {event['total_ms']:>7} ms\n"
            f"  model     {event['model_ms']:>7} ms  ({event['model_calls']} calls)\n"
            f"  tools     {event['tool_ms']:>7} ms  ({event['tool_calls']} calls)\n"
            f"  overhead  {max(0, event['total_ms'] - event['model_ms'] - event['tool_ms']):>7} ms\n"
            f"  tokens    in={event['input_tokens']} out={event['output_tokens']}",
            flush=True,
        )


def report_plan(plan: SearchPlan) -> None:
    print("\n\n--- created search ---")
    if plan.parse_error:
        print(f"could not read the created search: {plan.parse_error}")
    if plan.assumptions:
        print("assumptions: " + " | ".join(plan.assumptions))
    if plan.notes:
        print("notes: " + plan.notes)
    if plan.validation_errors:
        print("SCHEMA ERRORS (not runnable):")
        for err in plan.validation_errors:
            print(f"  - {err}")
    print(f"tool: {plan.tool}")
    print(json.dumps(plan.payload, indent=2) if plan.runnable else "no runnable search")

    if plan.analysis_instructions:
        print("\n--- analysis instructions ---")
        print(plan.analysis_instructions)
