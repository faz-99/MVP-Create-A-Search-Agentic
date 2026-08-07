"""Extract the search plan the build phase emits.

The build prompt asks for a trailing fenced JSON block. That is a prompt-level
contract rather than a schema-enforced one, so parsing is tolerant and failure is
explicit — a caller that gets `payload is None` must surface that rather than
proceeding with a half-formed plan.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _loads_or_none(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


@dataclass
class SearchPlan:
    tool: str | None = None
    payload: dict[str, Any] | None = None
    analysis_instructions: str = ""
    assumptions: list[str] = field(default_factory=list)
    notes: str = ""
    parse_error: str | None = None
    validation_errors: list[str] = field(default_factory=list)

    @property
    def runnable(self) -> bool:
        """Needs a target tool, arguments, and a payload that validates.

        An invalid payload is not runnable: letting Run stay enabled would just
        surface the schema error later, after the operator committed to it.
        """
        return (
            bool(self.tool)
            and isinstance(self.payload, dict)
            and not self.validation_errors
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "payload": self.payload,
            "analysis_instructions": self.analysis_instructions,
            "assumptions": self.assumptions,
            "notes": self.notes,
            "parse_error": self.parse_error,
            "validation_errors": self.validation_errors,
            "runnable": self.runnable,
        }


def _plan_shaped(candidate: Any) -> bool:
    """Does this object look like the plan rather than an incidental JSON blob?"""
    return isinstance(candidate, dict) and ("tool" in candidate or "payload" in candidate)


def _unfenced_objects(text: str) -> list[dict[str, Any]]:
    """Plan-shaped JSON objects in `text` that were not wrapped in a fence.

    The fence is a prompt-level contract, and weaker models honour it inconsistently —
    `gpt-oss` regularly emits the object bare. Refusing that is a self-inflicted
    failure: the search is right there, correctly formed, just not decorated.
    Restricted to plan-shaped objects so an argument blob quoted mid-explanation
    cannot be mistaken for the deliverable.
    """
    found: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if _plan_shaped(obj):
            found.append(obj)
    return found


def parse_plan(text: str) -> SearchPlan:
    """Pull the plan out of an assistant reply.

    Uses the *last* candidate: the model may quote an intermediate payload
    mid-explanation, and the final block is the one the prompt asks it to end with.
    Fenced blocks win; a bare object is accepted only if no fence carried a plan.
    """
    blocks = _FENCE.findall(text or "")
    fenced_plans = [b for b in blocks if _plan_shaped(_loads_or_none(b))]
    bare_plans = _unfenced_objects(text or "") if not fenced_plans else []

    if fenced_plans:
        raw = fenced_plans[-1]
    elif bare_plans:
        raw = json.dumps(bare_plans[-1])
    elif blocks:
        # A fence exists but carries no plan — keep the original diagnostics, which
        # distinguish malformed JSON from a block of the wrong shape.
        raw = blocks[-1]
    else:
        return SearchPlan(parse_error="No fenced JSON block found in the reply.")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return SearchPlan(parse_error=f"Final JSON block did not parse: {exc}")

    if not isinstance(data, dict):
        return SearchPlan(parse_error="Final JSON block was not an object.")

    payload = data.get("payload")
    if payload is not None and not isinstance(payload, dict):
        return SearchPlan(
            parse_error=f"`payload` was {type(payload).__name__}, expected an object."
        )

    tool = data.get("tool")
    if tool is not None and not isinstance(tool, str):
        return SearchPlan(
            parse_error=f"`tool` was {type(tool).__name__}, expected a string."
        )

    assumptions = data.get("assumptions") or []
    if not isinstance(assumptions, list):
        assumptions = [str(assumptions)]

    return SearchPlan(
        tool=tool,
        payload=payload,
        analysis_instructions=str(data.get("analysis_instructions") or ""),
        assumptions=[str(a) for a in assumptions],
        notes=str(data.get("notes") or ""),
    )
