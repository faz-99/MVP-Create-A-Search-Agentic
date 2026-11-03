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
from shared.llm import LLM, BedrockLLM, GatewayLLM, OpenAILLM
from shared.loop import run_build
from shared.mcp_client import MCPClient, MCPError
from shared.runner import execute_search
from shared.toolsets import available_execute_tools

STATIC_DIR = Path(__file__).parent / "static"


def pick_llm(provider: str, model: str | None = None) -> LLM:
    """Choose the adapter for a leg, with an optional model override from the picker.

    `claude` / `openai` run on Bedrock (the OpenAI leg via the OpenAI SDK once the
    deploy target's credentials exist). Any `gateway*` leg runs on the Intelligize
    gateway: it works without a bedrock:InvokeModel grant and is the **only** route to
    Gemini — neither Bedrock Converse nor the Anthropic SDK serves it. Needs the SSM
    tunnel open (see shared/config.GATEWAY_URL).

    `gateway-claude` / `gateway-openai` are still accepted so older links keep working;
    the model now comes from the picker rather than from the leg name.
    """
    chosen = (model or "").strip() or None

    if provider.startswith("gateway"):
        from shared.config import DEFAULT_GATEWAY_MODEL, gateway_account_type

        key = chosen or DEFAULT_GATEWAY_MODEL
        return GatewayLLM(key, gateway_account_type(key))

    if provider != "openai":
        return BedrockLLM(model_id=chosen)

    sdk = OpenAILLM(model=chosen)
    if sdk.available():
        return sdk
    return BedrockLLM(model_id=chosen or OPENAI_BEDROCK_MODEL_ID)

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
    # Empty/absent means the leg's default, so an older client still builds.
    model: str | None = None


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
            llm = pick_llm(request.provider, request.model)
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


@app.get("/api/providers")
async def providers() -> dict[str, Any]:
    """Which legs this instance can run, and which models each one offers.

    Asked rather than hardcoded so the gateway leg appears only where it works: on the
    deploy host there is no SSM tunnel, so this is the two Bedrock legs the UI has
    always shown.

    The Bedrock legs each report a single model because the id is fixed by the
    environment, and because the prefix rule differs by family (`us.` required for
    Anthropic, rejected for gpt-oss) — an unverified id there fails at invoke time, so
    the picker offers only what has been checked. Model choice is real on the gateway.
    """
    from shared.config import (
        BEDROCK_MODEL_ID,
        DEFAULT_GATEWAY_MODEL,
        GATEWAY_LEGS_ENABLED,
        GATEWAY_MODELS,
        GATEWAY_URL,
        OPENAI_MODEL,
    )

    # Report what the OpenAI leg will actually run here, which depends on whether the
    # deploy target's credentials are present.
    openai_model = OPENAI_MODEL if OpenAILLM().available() else OPENAI_BEDROCK_MODEL_ID

    legs: list[dict[str, Any]] = [
        {
            "value": "claude",
            "label": "Claude SDK",
            "models": [{"value": BEDROCK_MODEL_ID, "label": BEDROCK_MODEL_ID}],
            "default": BEDROCK_MODEL_ID,
        },
        {
            "value": "openai",
            "label": "OpenAI SDK",
            "models": [{"value": openai_model, "label": openai_model}],
            "default": openai_model,
        },
    ]
    if GATEWAY_LEGS_ENABLED:
        legs.append(
            {
                "value": "gateway",
                "label": "Gateway",
                "note": f"via {GATEWAY_URL}",
                "models": [
                    {"value": key, "label": str(entry["label"])}
                    for key, entry in GATEWAY_MODELS.items()
                ],
                "default": DEFAULT_GATEWAY_MODEL,
            }
        )
    # Open on the gateway where it exists, so the default run is Gemini 3 Flash rather
    # than the Bedrock leg. On the deploy host there is no gateway and this is absent,
    # leaving the Claude leg selected as before.
    selected = "gateway" if GATEWAY_LEGS_ENABLED else "claude"
    return {"ok": True, "providers": legs, "selected": selected}


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
