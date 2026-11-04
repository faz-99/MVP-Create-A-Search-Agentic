"""The system prompt, shared byte-for-byte by both loops.

Keep prompts/system.txt provider-neutral: if one model needs different wording to
behave, that is a finding about the prompt, not a reason to fork it.
"""

from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "system.txt"


@lru_cache(maxsize=1)
def _base_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


def build_instructions(apps: list[str]) -> str:
    """Date and app context first, as agentv4 does, then the fixed prompt."""
    today = datetime.now(timezone.utc).date().isoformat()
    names = ", ".join(apps)
    return "\n\n".join(
        [
            f"Today is {today}.",
            f"Applications available for this request, starting application first: {names}.",
            _base_prompt(),
        ]
    )
