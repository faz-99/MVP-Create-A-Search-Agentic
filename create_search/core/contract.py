"""The output contract, and the two agent-only tools that produce it.

The deliverable is one executable search, delivered through `submit_search` rather than
a JSON block at the end of a reply: the tool validates the payload and hands any errors
straight back to the model, so repair is just the next turn, identical on both loops.

`report_strategy` exists so the UI can show the Search Strategy early, as Create Search
does today, before the filters are final.
"""

from dataclasses import asdict, dataclass, field
from typing import Any

SUBMIT_TOOL = "submit_search"
STRATEGY_TOOL = "report_strategy"
AGENT_TOOLS = frozenset({SUBMIT_TOOL, STRATEGY_TOOL})


@dataclass
class CreatedSearch:
    strategy: str = ""
    tool: str | None = None
    payload: dict[str, Any] | None = None
    analysis_instructions: str = ""
    assumptions: list[str] = field(default_factory=list)
    unresolved_conditions: list[str] = field(default_factory=list)
    notes: str = ""
    validation_errors: list[str] = field(default_factory=list)
    # "agent" when submit_search accepted it, "fallback" when limits forced the last
    # valid search the agent ran, None when there is nothing to run.
    source: str | None = None

    @property
    def runnable(self) -> bool:
        return bool(self.tool) and isinstance(self.payload, dict) and not self.validation_errors

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "runnable": self.runnable}


def strategy_tool_def() -> dict[str, Any]:
    return {
        "name": STRATEGY_TOOL,
        "description": (
            "Report the user-facing search strategy as soon as you have one, before you "
            "finish building filters. Call it once, early. The final strategy goes in "
            "submit_search and may refine this one."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "strategy": {
                    "type": "string",
                    "description": (
                        "Two to five plain sentences for the user: which dataset, which "
                        "filters and why, and how the request was interpreted."
                    ),
                }
            },
            "required": ["strategy"],
            "additionalProperties": False,
        },
    }


def submit_tool_def(search_tools: list[str]) -> dict[str, Any]:
    return {
        "name": SUBMIT_TOOL,
        "description": (
            "Submit the final search. This is the only way to deliver it. The payload is "
            "validated against the named search tool's schema; if it is rejected you get "
            "the errors back and must call this again with a corrected payload."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "strategy": {
                    "type": "string",
                    "description": "Final user-facing search strategy, plain sentences.",
                },
                "tool": {
                    "type": "string",
                    "enum": search_tools,
                    "description": "The search tool this payload runs against.",
                },
                "payload": {
                    "type": "object",
                    "description": "Exact arguments for that tool, valid as-is.",
                },
                "analysis_instructions": {
                    "type": "string",
                    "description": "Instructions for analysing the returned documents.",
                },
                "assumptions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Readings chosen where the request was ambiguous.",
                },
                "unresolved_conditions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Parts of the request this search does not cover, and why.",
                },
                "notes": {"type": "string"},
            },
            "required": ["strategy", "tool", "payload", "analysis_instructions"],
            "additionalProperties": False,
        },
    }
