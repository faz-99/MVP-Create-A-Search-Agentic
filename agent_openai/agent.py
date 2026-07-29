"""Build phase — OpenAI.

Runs the client-side tool loop (shared/loop.py). The search executors are never
registered, so this cannot run a search — its deliverable is the created search plus
the analysis instructions.

Shares the master prompt and the loop with the Claude agent; only the model adapter
differs. Every tool call and result is written to out/.

Runs on Bedrock-hosted gpt-oss using the same AWS credentials as the Claude agent —
no OpenAI key needed. Note these are the open-weight gpt-oss models, not GPT-5.x.
`shared.llm.OpenAILLM` remains available if a direct OpenAI key is added later.

    python -m agent_openai.agent "8-K filings where the CEO resigned"
    python -m agent_openai.agent --run "..."     # also execute the search

Without --run this is build-only, matching the UI's default gate. Analysis is a
separate existing API and is not run from here (see shared/analysis.py).
"""

import json
import sys

from shared.cli import report_plan, show_event
from shared.config import OPENAI_BEDROCK_MODEL_ID
from shared.llm import BedrockLLM
from shared.loop import run_build
from shared.runner import execute_search


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--run"]
    should_run = "--run" in sys.argv
    if not args:
        print(__doc__)
        return 2

    llm = BedrockLLM(model_id=OPENAI_BEDROCK_MODEL_ID)
    print(f"model: {llm.name}\n")
    plan = run_build(llm, " ".join(args), on_event=show_event)
    report_plan(plan)

    if not should_run:
        if plan.runnable:
            print("\n(build only — pass --run to execute the search)")
        return 0
    if not plan.runnable:
        print("\nnothing to run.")
        return 1

    print("\n--- running search ---")
    outcome = execute_search(plan.tool, plan.payload)
    if not outcome.ok:
        print(f"run failed: {outcome.error}")
        return 1
    print(f"executed via {outcome.tool}\n")
    print(json.dumps(outcome.result, indent=2, default=repr)[:3000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
