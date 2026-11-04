"""Validate a payload against the live input schema of its search tool.

The schemas set `additionalProperties: false`, so invented field names are caught
mechanically. Messages are written for the model to act on: an unknown field names the
valid ones, since that is what it needs to fix the payload.
"""

from typing import Any

from jsonschema import Draft202012Validator


def validate_payload(tool: str, payload: Any, schema: dict[str, Any] | None) -> list[str]:
    """Human-readable errors. Empty list means valid."""
    if schema is None:
        return [f"'{tool}' is not a search tool available to this request."]
    if not isinstance(payload, dict):
        return [f"payload must be an object, got {type(payload).__name__}."]

    errors: list[str] = []
    properties = schema.get("properties", {})
    for error in sorted(
        Draft202012Validator(schema).iter_errors(payload), key=lambda e: list(e.path)
    ):
        if error.validator == "additionalProperties":
            unknown = sorted(set(payload) - set(properties))
            errors.append(
                f"unknown field(s) {unknown} for '{tool}'. "
                f"Valid fields: {', '.join(sorted(properties))}"
            )
            continue
        where = ".".join(str(p) for p in error.path) or "(root)"
        errors.append(f"{where}: {error.message}")
    return errors
