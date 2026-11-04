"""Streamed events, in the same shape agentv4 emits: {"kind": ..., "message": ..., ...}.

Kinds:
  status      progress line for the UI
  strategy    the user-facing Search Strategy (early, then final)
  trace       a tool call or tool output (message is display text)
  reasoning   model reasoning summary / thinking text
  warning     something degraded but the run continues
  step        one timed model or tool step (message is the step dict)
  usage       token totals (message is a JSON string, as in agentv4)
  metrics     per-step timings and totals
  done        final event; message is CreatedSearch.to_dict()
  agenterror  the run failed; message is the error text
  stop        the run was stopped on request
  ping        keep-alive for long silent stretches
"""

from typing import Any, Callable

Event = dict[str, Any]
Emit = Callable[[Event], None]


def event(kind: str, message: Any = "", **extra: Any) -> Event:
    return {"kind": kind, "message": message, **extra}
