"""FastAPI backend for the search-agent UI.

Three routes mirroring the three phases:

  POST /api/build    phase 1 — streams thinking, tool calls and text, ends with a
                     plan (payload + analysis instructions). Never runs a search.
  POST /api/run      phase 2 — user-gated. Calls the MCP execute tool directly with
                     the payload. Deterministic; no model involved.
  POST /api/analyze  phase 3 — user-gated. Streams the analysis of the results.

Both providers share one master prompt (shared/prompts.py) and one gate
(shared/toolsets.py), so switching provider changes only who drives the loop.

    uvicorn ui.server:app --reload --port 8000
"""

import json
import queue
import threading
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from shared.analysis import run_analysis
from shared.config import MCP_SERVER_NAME, OPENAI_BEDROCK_MODEL_ID
from shared.llm import LLM, BedrockLLM, OpenAILLM
from shared.loop import run_build
from shared.mcp_client import MCPClient, MCPError
from shared.runner import execute_search
from shared.toolsets import available_execute_tools

STATIC_DIR = Path(__file__).parent / "static"


def pick_llm(provider: str) -> LLM:
    """Choose the adapter for a leg.

    The OpenAI leg is meant to run through the OpenAI SDK against an AWS-hosted
    OpenAI-compatible endpoint, which only works on the deploy target. Locally there
    is no credential for it, so it falls back to the same model over Bedrock Converse
    rather than erroring — the leg stays exercisable in dev, and switches to the SDK
    path automatically once deployed.
    """
    if provider != "openai":
        return BedrockLLM()

    sdk = OpenAILLM()
    if sdk.available():
        return sdk
    return BedrockLLM(model_id=OPENAI_BEDROCK_MODEL_ID)

app = FastAPI(title="AI Create Search")

_tool_cache: list[dict[str, Any]] | None = None


def _available_tools() -> list[dict[str, Any]]:
    """tools/list, cached — the surface is static for a server process."""
    global _tool_cache
    if _tool_cache is None:
        with MCPClient() as mcp:
            _tool_cache = mcp.list_tools()
    return _tool_cache


def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=repr)}\n\n"


# --- request bodies -----------------------------------------------------------


class BuildRequest(BaseModel):
    query: str
    provider: str = "claude"


class RunRequest(BaseModel):
    tool: str
    payload: dict[str, Any]


class AnalyzeRequest(BaseModel):
    payload: dict[str, Any]
    results: Any
    analysis_instructions: str


# --- phase 1: build (client-side tool loop) -----------------------------------
# The loop runs here, not provider-side: MCP auth is a custom `lna` header that no
# hosted MCP connector can send. Same loop for both providers.


@app.post("/api/build")
async def build(request: BuildRequest) -> StreamingResponse:
    def generate() -> Iterator[str]:
        try:
            llm = pick_llm(request.provider)
        except Exception as exc:
            yield sse({"type": "error", "message": str(exc)})
            yield sse({"type": "done"})
            return

        yield sse({"type": "status", "text": f"Creating search with {llm.name}…"})

        # The loop is synchronous and long-running. Run it on a thread and drain a
        # queue so thinking and tool calls reach the browser as they happen —
        # collecting events into a list first would defeat the point of streaming.
        events: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["plan"] = run_build(llm, request.query, on_event=events.put)
            except Exception as exc:
                box["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                events.put(None)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()

        while True:
            event = events.get()
            if event is None:
                break
            yield sse(event)

        thread.join()
        if "error" in box:
            yield sse({"type": "error", "message": box["error"]})
        else:
            yield sse({"type": "plan", "plan": box["plan"].to_dict()})
        yield sse({"type": "done"})

    return StreamingResponse(generate(), media_type="text/event-stream")


# --- phase 2: run (user-gated, no model) --------------------------------------


@app.post("/api/run")
async def run(request: RunRequest) -> dict[str, Any]:
    outcome = execute_search(request.tool, request.payload)
    return {
        "ok": outcome.ok,
        "tool": outcome.tool,
        "result": outcome.result,
        "error": outcome.error,
    }


# --- phase 3: analyze (user-gated, external API) ------------------------------
# No prompt and no model call here: analysis is an existing internal API, wired in
# at the seam in shared/analysis.py. This route only forwards and reports.


@app.post("/api/analyze")
async def analyze(request: AnalyzeRequest) -> dict[str, Any]:
    result = run_analysis(
        request.payload, request.results, request.analysis_instructions
    )
    return {"ok": result.ok, "output": result.output, "error": result.error}



# --- misc ---------------------------------------------------------------------


@app.get("/api/tools")
async def tools() -> dict[str, Any]:
    try:
        available = _available_tools()
    except MCPError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "server": MCP_SERVER_NAME,
        "withheld_execute_tools": available_execute_tools(available),
        "tools": [
            {
                "name": t.get("name"),
                "description": (t.get("description") or "").split("\n")[0],
            }
            for t in available
        ],
    }


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
