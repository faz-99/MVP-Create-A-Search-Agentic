"""Shrink tool results before the model sees them.

A search run during the build only has to tell the agent whether its filters are right:
the total, and the first few rows' metadata. Nested lists become counts and long text is
cut, so a result costs a few hundred tokens instead of several thousand. The full result
is still written to the run capture.
"""

import json
from typing import Any

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
    """Total + first `rows` result rows with scalar fields only."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return truncate(text, 2000)
    if not isinstance(data, dict):
        return truncate(text, 2000)

    results = data.get("results")
    summary: dict[str, Any] = {k: _short(v) for k, v in data.items() if k != "results"}
    # Not every dataset returns `total`; say so rather than letting the model assume 0.
    summary.setdefault("total", "not reported by this dataset")
    if isinstance(results, list):
        summary["rows_returned"] = len(results)
        summary["rows"] = [
            {k: _short(v) for k, v in row.items()} if isinstance(row, dict) else _short(row)
            for row in results[:rows]
        ]
    return json.dumps(summary, ensure_ascii=False)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[cut: {len(text) - limit} more characters. Narrow the query.]"
