"""HTTP API for the Create Search agent (python -m uvicorn api.server:app --port 8080).

POST /api/create-search (SSE) · PUT /api/create-search/stop · POST /api/run · GET /api/models · GET /api/tools · GET /api/health · GET /
Auth as agentv4: lna cookie, then lna header, then (non-prod) LEXIS_SSO_COOKIE.
"""

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from create_search import config
from create_search.agent import AgentRequest, create_agent
from create_search.clients import available_models, close_clients, list_tool_dicts, open_mcp
from create_search.toolset import is_search_tool, normalize_apps, select_tools
from create_search.utils import request_stop, stop_key, validate_payload

PING_SECONDS = 5
INDEX_HTML = Path(__file__).resolve().parent.parent / "ui" / "index.html"  # test page, served at /
PROD = config.ENV.lower() == "prod"


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        yield
    finally:
        await close_clients()


app = FastAPI(lifespan=lifespan, **({"docs_url": None, "redoc_url": None, "openapi_url": None} if PROD else {}))
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("ORIGINS", "*").split("|"), allow_credentials=True, allow_headers=["*"], allow_methods=["*"])


class CreateSearchRequest(BaseModel):
    Query: str
    Id: str | None = None
    Provider: Literal["openai", "claude"] = "openai"
    Model: str | None = None
    Apps: list[str] | None = None
    Effort: Literal["None", "Low", "Medium", "High", "XHigh", "Max"] = "Medium"


class RunRequest(BaseModel):
    Tool: str
    Payload: dict[str, Any]


def get_lna_value(req: Request) -> str:
    token = req.cookies.get(config.AUTH_HEADER, "") or req.headers.get(config.AUTH_HEADER, "")
    if not token.strip() and not PROD:
        token = config.dev_token()
    if not token.strip():
        raise HTTPException(status.HTTP_403_FORBIDDEN)
    return token


def _user_key(token: str) -> str:
    """Per-user folder for stop files, without storing the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


async def _sse(events: AsyncIterator[dict[str, Any]]) -> AsyncIterator[str]:
    """Events as SSE frames, with a ping while the agent is quiet."""
    queue: asyncio.Queue = asyncio.Queue()
    done = object()

    async def pump() -> None:
        try:
            async for ev in events:
                await queue.put(ev)
        except Exception as exc:  # keep the stream well-formed
            await queue.put({"kind": "agenterror", "message": str(exc)})
        finally:
            await queue.put(done)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=PING_SECONDS)
            except asyncio.TimeoutError:
                yield "data: " + json.dumps({"kind": "ping"}) + "\n\n"
                continue
            if item is done:
                break
            yield "data: " + json.dumps(item, ensure_ascii=False, default=str) + "\n\n"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@app.post("/api/create-search")
async def create_search_endpoint(body: CreateSearchRequest, req: Request):
    token = get_lna_value(req)
    run_id = body.Id or ""
    request = AgentRequest(Prompt=body.Query, ConversationId=run_id, Effort=body.Effort.lower())
    events = create_agent(token, body.Model).stream_research(request, provider=body.Provider, apps=body.Apps,
                                                             stop_key=stop_key(_user_key(token), run_id) if run_id else None)
    headers = {"Cache-Control": "no-cache, no-transform", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
    return StreamingResponse(_sse(events), media_type="text/event-stream", headers=headers)


@app.put("/api/create-search/stop", status_code=status.HTTP_204_NO_CONTENT)
async def stop_endpoint(id: str, req: Request):
    request_stop(stop_key(_user_key(get_lna_value(req)), id))


@app.post("/api/run")
async def run_endpoint(body: RunRequest, req: Request):
    """Run a created (possibly edited) payload; validated again first."""
    if not is_search_tool(body.Tool):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"'{body.Tool}' is not a search tool.")
    async with open_mcp(get_lna_value(req)) as server:
        schema = {t["name"]: t for t in await list_tool_dicts(server)}.get(body.Tool, {}).get("inputSchema")
        if errors := validate_payload(body.Tool, body.Payload, schema):
            raise HTTPException(422, {"validation_errors": errors})
        result = await server.call_tool(body.Tool, body.Payload)
    return result.model_dump(mode="json")


@app.get("/api/models")
async def models_endpoint():
    return await asyncio.to_thread(available_models)


@app.get("/api/tools")
async def tools_endpoint(req: Request, apps: str = "SF,AOE"):
    try:
        selected = normalize_apps(apps.split(","))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    async with open_mcp(get_lna_value(req)) as server:
        toolset = select_tools(await list_tool_dicts(server), selected)
    return {"apps": selected, "search_tools": toolset.search_tools, "tools": sorted(toolset.tools)}


@app.get("/api/health")
async def health():
    return {"ModuleName": "Create Search Agent"}


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(INDEX_HTML)
