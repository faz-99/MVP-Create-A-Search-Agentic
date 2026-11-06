"""Small helpers shared by the loops: events, stop files, prompt, limit messages, validation, trimming, run recorder."""

import json
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator

from create_search import config
from create_search.result import SUBMIT_TOOL

# --- Events: agentv4 shape {"kind", "message", ...} -------------------------------------------------------------
# Kinds: status, strategy, trace, reasoning, warning, step, usage (JSON string), metrics, done, agenterror, stop, ping.
Event = dict[str, Any]
Emit = Callable[[Event], None]


def event(kind: str, message: Any = "", **extra: Any) -> Event:
    return {"kind": kind, "message": message, **extra}


# --- Stop requests: file-based, as agentv4 (PUT /stop touches a file the loop polls) -----------------------------
def stop_key(user_id: str, run_id: str) -> Path:
    return Path(config.STORAGE_PATH) / "stops" / user_id / run_id / ".stop"


def request_stop(key: Path) -> None:
    key.parent.mkdir(parents=True, exist_ok=True)
    key.touch(exist_ok=True)


def is_stop_requested(key: Path | None) -> bool:
    return key is not None and key.exists()


def clear_stop(key: Path | None) -> None:
    if key is not None and key.exists():
        key.unlink()


# --- System prompt: date and apps first (as agentv4), then prompts/system.txt ------------------------------------
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "system.txt"


@lru_cache(maxsize=1)
def _base_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


def build_instructions(apps: list[str]) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    return f"Today is {today}.\n\nApplications available for this request, starting application first: {', '.join(apps)}.\n\n{_base_prompt()}"


# --- Limit messages ----------------------------------------------------------------------------------------------
def turn_notice(turn: int, max_turns: int) -> str | None:
    remaining = max_turns - turn
    if 0 < remaining <= config.WARN_BEFORE:
        return (f"[SYSTEM NOTICE] {remaining} turn(s) left before this run is stopped. Stop exploring and call {SUBMIT_TOOL} now "
                "with the best valid search you have, listing anything unresolved in unresolved_conditions.")
    return None


def submit_reminder() -> str:
    return (f"You ended your reply without calling {SUBMIT_TOOL}. The search is only delivered through that tool. "
            f"Call {SUBMIT_TOOL} now; do not ask the user anything and do not restate your reasoning.")


# --- Validation against the tool's live schema, with errors the model can act on ---------------------------------
def validate_payload(tool: str, payload: Any, schema: dict[str, Any] | None) -> list[str]:
    """Error messages; empty means valid."""
    if schema is None:
        return [f"'{tool}' is not a search tool available to this request."]
    if not isinstance(payload, dict):
        return [f"payload must be an object, got {type(payload).__name__}."]

    properties = schema.get("properties", {})
    errors: list[str] = []
    for error in sorted(Draft202012Validator(schema).iter_errors(payload), key=lambda e: list(e.path)):
        if error.validator == "additionalProperties":
            errors.append(f"unknown field(s) {sorted(set(payload) - set(properties))} for '{tool}'. Valid fields: {', '.join(sorted(properties))}")
        else:
            errors.append(f"{'.'.join(str(p) for p in error.path) or '(root)'}: {error.message}")
    return errors


# --- Trimming: search runs become total + a few scalar rows; long text is cut -----------------------------------
MAX_FIELD_CHARS = 200


def _short(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
        return value[:MAX_FIELD_CHARS] + "…"
    if isinstance(value, list):
        return f"[{len(value)} item(s)]"
    if isinstance(value, dict):
        return f"{{{len(value)} field(s)}}"
    return value


def summarize_search(text: str, rows: int) -> str:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return truncate(text, 2000)
    if not isinstance(data, dict):
        return truncate(text, 2000)

    results = data.get("results")
    summary: dict[str, Any] = {k: _short(v) for k, v in data.items() if k != "results"}
    summary.setdefault("total", "not reported by this dataset")  # so the model doesn't assume 0
    if isinstance(results, list):
        summary["rows_returned"] = len(results)
        summary["rows"] = [{k: _short(v) for k, v in r.items()} if isinstance(r, dict) else _short(r) for r in results[:rows]]
    return json.dumps(summary, ensure_ascii=False)


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n…[cut: {len(text) - limit} more characters. Narrow the query.]"


# --- Run recorder: timing, tokens and a full capture of every tool call to out/ (writing never raises) -------------
class RunRecorder:
    def __init__(self, provider: str, model: str, query: str, apps: list[str]):
        self.provider, self.model, self.query, self.apps = provider, model, query, apps
        self.steps: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.started = time.perf_counter()
        self.write_error: str | None = None

    def step(self, kind: str, label: str, seconds: float, **extra: Any) -> dict[str, Any]:
        entry = {"step": len(self.steps) + 1, "kind": kind, "label": label, "ms": round(seconds * 1000), **extra}
        self.steps.append(entry)
        return entry

    def call(self, call_id: str, tool: str, arguments: Any, result: str, error: bool) -> None:
        self.calls.append({"call_id": call_id, "tool": tool, "arguments": arguments, "result": result, "error": error})

    def totals(self) -> dict[str, Any]:
        model = [s for s in self.steps if s["kind"] == "model"]
        tools = [s for s in self.steps if s["kind"] == "tool"]
        return {
            "steps": self.steps,
            "total_ms": round((time.perf_counter() - self.started) * 1000),
            "model_ms": sum(s["ms"] for s in model),
            "tool_ms": sum(s["ms"] for s in tools),  # tools run in parallel, so this can exceed wall-clock
            "model_calls": len(model),
            "tool_calls": len(tools),
            **{k: sum(s.get(k, 0) for s in model) for k in ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens")},
        }

    def write(self, result: dict[str, Any] | None) -> None:
        path = Path(config.CAPTURE_DIR) / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-cs-{self.provider}.json"
        capture = {"provider": self.provider, "model": self.model, "query": self.query, "apps": self.apps,
                   "result": result, "metrics": self.totals(), "calls": self.calls}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(capture, indent=2, ensure_ascii=False, default=repr), encoding="utf-8")
        except OSError as exc:
            self.write_error = f"could not write {path}: {exc}"
