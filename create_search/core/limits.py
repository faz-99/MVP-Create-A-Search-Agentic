"""Messages the loops inject when a limit is near or a reply misses the deliverable.

The limits themselves are enforced in code (config.py values, checked by the executor
and the loops); these only tell the model what is happening so it can finalize cleanly.
"""

from create_search.config import WARN_BEFORE
from create_search.core.contract import SUBMIT_TOOL


def turn_notice(turn: int, max_turns: int) -> str | None:
    """Wrap-up notice once few turns remain, as agentv4's ContextSizeHooks does."""
    remaining = max_turns - turn
    if 0 < remaining <= WARN_BEFORE:
        return (
            f"[SYSTEM NOTICE] {remaining} turn(s) left before this run is stopped. "
            f"Stop exploring and call {SUBMIT_TOOL} now with the best valid search you "
            "have, listing anything unresolved in unresolved_conditions."
        )
    return None


def submit_reminder() -> str:
    return (
        f"You ended your reply without calling {SUBMIT_TOOL}. The search is only "
        f"delivered through that tool. Call {SUBMIT_TOOL} now; do not ask the user "
        "anything and do not restate your reasoning."
    )
