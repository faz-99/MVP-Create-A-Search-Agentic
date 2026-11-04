"""Per-run timing, token counts and a capture of every tool call.

Timing lives here, not in the model adapters, so both loops are measured the same way.
The capture keeps full (untrimmed) tool results in out/ so runs can be diffed across
providers. Writing it never raises: a lost log file must not cost the deliverable.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from create_search.config import CAPTURE_DIR


class RunRecorder:
    def __init__(self, provider: str, model: str, query: str, apps: list[str]):
        self.provider = provider
        self.model = model
        self.query = query
        self.apps = apps
        self.steps: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.started = time.perf_counter()
        self.write_error: str | None = None

    def step(self, kind: str, label: str, seconds: float, **extra: Any) -> dict[str, Any]:
        entry = {
            "step": len(self.steps) + 1,
            "kind": kind,
            "label": label,
            "ms": round(seconds * 1000),
            **extra,
        }
        self.steps.append(entry)
        return entry

    def call(self, call_id: str, tool: str, arguments: Any, result: str, error: bool) -> None:
        self.calls.append(
            {"call_id": call_id, "tool": tool, "arguments": arguments, "result": result, "error": error}
        )

    def totals(self) -> dict[str, Any]:
        model_steps = [s for s in self.steps if s["kind"] == "model"]
        tool_steps = [s for s in self.steps if s["kind"] == "tool"]
        total_ms = round((time.perf_counter() - self.started) * 1000)
        model_ms = sum(s["ms"] for s in model_steps)
        return {
            "steps": self.steps,
            "total_ms": total_ms,
            "model_ms": model_ms,
            # Tools may run in parallel, so tool_ms can exceed their wall-clock share.
            "tool_ms": sum(s["ms"] for s in tool_steps),
            "model_calls": len(model_steps),
            "tool_calls": len(tool_steps),
            "input_tokens": sum(s.get("input_tokens", 0) for s in model_steps),
            "cached_tokens": sum(s.get("cached_tokens", 0) for s in model_steps),
            "output_tokens": sum(s.get("output_tokens", 0) for s in model_steps),
            "reasoning_tokens": sum(s.get("reasoning_tokens", 0) for s in model_steps),
        }

    def write(self, result: dict[str, Any] | None) -> Path | None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = Path(CAPTURE_DIR) / f"{stamp}-cs-{self.provider}.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "provider": self.provider,
                        "model": self.model,
                        "query": self.query,
                        "apps": self.apps,
                        "result": result,
                        "metrics": self.totals(),
                        "calls": self.calls,
                    },
                    indent=2,
                    ensure_ascii=False,
                    default=repr,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            self.write_error = f"could not write {path}: {exc}"
            return None
        return path
