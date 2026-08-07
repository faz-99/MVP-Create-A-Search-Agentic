"""Capture the /Search payloads an agent sends and the results it gets back.

The payload is a deliverable, not an intermediate — it has to be recorded exactly as
sent, independently of how the model chose to summarise it in prose. Both agents write
the same file shape so runs can be diffed across providers.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OUT_DIR = Path("out")


class RunCapture:
    """Accumulates tool calls for one user turn, then writes them to out/."""

    def __init__(self, provider: str, model: str, request: str):
        self.provider = provider
        self.model = model
        self.request = request
        self.calls: list[dict[str, Any]] = []

        # Why write() failed, for the caller to surface. None until it does.
        self.write_error: str | None = None

    def record_call(
        self,
        name: str,
        arguments: Any,
        *,
        call_id: str | None = None,
    ) -> None:
        self.calls.append(
            {
                "call_id": call_id,
                "tool": name,
                "payload": _jsonable(arguments),
                "result": None,
                "error": None,
            }
        )

    def record_result(
        self,
        result: Any,
        *,
        call_id: str | None = None,
        error: Any = None,
    ) -> None:
        """Attach a result to its call — by id when available, else the latest call."""
        target = None
        if call_id is not None:
            target = next((c for c in self.calls if c["call_id"] == call_id), None)
        if target is None:
            target = next((c for c in reversed(self.calls) if c["result"] is None), None)
        if target is None:
            return
        target["result"] = _jsonable(result)
        target["error"] = _jsonable(error) if error else None

    def write(self) -> Path | None:
        """Write the run to out/. Returns the path, or None if nothing was written.

        Never raises: the only call site is a `finally`, where an exception replaces
        the returned plan. An unwritable out/ on the deploy host (bind mount owned by
        another uid) cost the UI its Run button that way. Reason goes in write_error.
        """
        if not self.calls:
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = OUT_DIR / f"{stamp}-{self.provider}.json"
        try:
            OUT_DIR.mkdir(exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "provider": self.provider,
                        "model": self.model,
                        "request": self.request,
                        "calls": self.calls,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            # PermissionError, ENOSPC, read-only fs — all the same from here.
            self.write_error = f"could not write {path}: {exc}"
            return None
        return path


def _jsonable(value: Any) -> Any:
    """Coerce SDK objects to something json.dumps can handle, losslessly if possible."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    for attr in ("model_dump", "to_dict"):
        method = getattr(value, attr, None)
        if callable(method):
            try:
                return _jsonable(method())
            except Exception:
                pass
    return repr(value)
