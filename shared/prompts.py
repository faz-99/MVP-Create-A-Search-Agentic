"""Loader for the prompt text files in prompts/.

SINGLE SOURCE OF TRUTH FOR BOTH SDKs. `BUILD_SYSTEM_PROMPT` is the master prompt:
Claude passes it as `system=`, OpenAI as `instructions=` — the same bytes either way.
Keep the text provider-neutral. Nothing in it may name a model, an SDK, or a
provider-specific mechanism; if one provider needs different wording to behave, that
is a finding about the prompt, not a licence to fork it. A forked prompt makes the two
agents incomparable, which defeats the point of running both.

The prompts live as .txt so they can be edited without touching Python:

  prompts/build_system.txt    build the payload, author analysis instructions.
                              Includes a {{PLAN_FENCE}} placeholder.
  prompts/plan_fence.txt      the required output contract, substituted in above.

There is deliberately no analysis prompt. Running the analysis is an existing
internal API — see shared/analysis.py. The build phase only *authors* the
instructions; it does not execute them, and neither do we.
"""

from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load(name: str) -> str:
    path = PROMPT_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt file missing: {path}")
    return path.read_text(encoding="utf-8").strip()


def _build_prompt() -> str:
    text = load("build_system.txt")
    fence = load("plan_fence.txt")
    if "{{PLAN_FENCE}}" not in text:
        raise ValueError(
            "prompts/build_system.txt is missing the {{PLAN_FENCE}} placeholder — "
            "without it the model is never told to emit a parseable plan."
        )
    return text.replace("{{PLAN_FENCE}}", fence)


BUILD_SYSTEM_PROMPT = _build_prompt()
