"""Phase 3 — integration seam for the existing analysis API.

Analysis is NOT implemented here by design. There is an existing internal API that
already does it, prompt included, and it will be wired in at this seam. Nothing in
this file should grow a prompt, a model call, or analysis logic of its own — a
second implementation would just diverge from the real one.

What the rest of the codebase guarantees when that API arrives:

  payload      the /Search request body that was actually run (dict)
  results      whatever the execute tool returned, verbatim
  instructions the analysis instructions the build phase authored (str)

To integrate: implement `run_analysis` against the real endpoint and delete
`ANALYSIS_NOT_INTEGRATED`. Callers already handle `AnalysisResult.ok == False`, so
no caller changes are needed.
"""

from dataclasses import dataclass
from typing import Any

ANALYSIS_NOT_INTEGRATED = (
    "Analysis is not wired up yet. The search ran and the results above are real; "
    "the analysis step will call the existing internal analysis API once its "
    "endpoint is available. Implement shared/analysis.py:run_analysis to enable it."
)


@dataclass
class AnalysisResult:
    ok: bool
    output: Any = None
    error: str | None = None


def run_analysis(
    payload: dict[str, Any], results: Any, instructions: str
) -> AnalysisResult:
    """Hand the run off to the existing analysis API.

    Stubbed until that endpoint is available. Returns a not-ok result rather than
    raising, so the UI can render the reason in place instead of erroring out.
    """
    del payload, results, instructions  # accepted now, forwarded once wired up
    return AnalysisResult(ok=False, error=ANALYSIS_NOT_INTEGRATED)
