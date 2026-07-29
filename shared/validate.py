"""Validate a created search against the real tool schema.

"Looks plausible" is not good enough: the first run of this agent emitted
`formTypeIds` / `query` / `limit`, none of which exist. The schemas declare
`additionalProperties: false`, so every one of those was mechanically detectable
before anything was run — we just weren't checking.

Validation happens twice, deliberately:
  - in the build loop, where a failure is repairable (the model gets the errors and
    another turn);
  - in the runner, as a last guard before a call actually goes out.
"""

from typing import Any

from jsonschema import Draft202012Validator


def schema_for(tool: str, available: list[dict[str, Any]]) -> dict[str, Any] | None:
    for entry in available:
        if entry.get("name") == tool:
            return entry.get("inputSchema") or {}
    return None


def validate_payload(
    tool: str, payload: Any, available: list[dict[str, Any]]
) -> list[str]:
    """Return human-readable errors. Empty list means valid."""
    if not tool:
        return ["No tool named — cannot validate."]

    schema = schema_for(tool, available)
    if schema is None:
        known = sorted(e.get("name", "?") for e in available)
        return [f"'{tool}' is not a tool this server advertises. Known: {', '.join(known[:8])}…"]
    if not isinstance(payload, dict):
        return [f"payload must be an object, got {type(payload).__name__}."]

    errors: list[str] = []
    for error in sorted(
        Draft202012Validator(schema).iter_errors(payload), key=lambda e: list(e.path)
    ):
        where = ".".join(str(p) for p in error.path) or "(root)"

        # The default message for additionalProperties names the offending key but not
        # what to use instead, which is the thing the model needs to hear.
        if error.validator == "additionalProperties":
            unknown = sorted(set(payload) - set(schema.get("properties", {})))
            valid = sorted(schema.get("properties", {}))
            errors.append(
                f"unknown field(s) {unknown} — not in the schema for '{tool}'. "
                f"Valid fields: {', '.join(valid)}"
            )
            continue

        errors.append(f"{where}: {error.message}")

    return errors
