# Adding an agent under `src`

This guide explains how to add a specialized agent to this application using the existing `ProtegeAgentCore` architecture. It is for contributors who are new to the codebase and want to know which application files to add or change, and which behavior the shared core already provides.

## Architecture at a glance

The current chat path is:

1. `api.py` builds a service for the request. Its `request_context` creates an agent with `src/agent_helper.py:create_agent` and wraps it in `FinancialApiService`.
2. The `/api/chat` route starts `ApiService.run_stream` and returns the queued events as a server-sent event stream.
3. `protege_agent_core.protege_base_service.ApiService` turns the chat request into an `AgentRequest`, obtains service-specific additional data, and calls the agent's `stream_research` method.
4. `protege_agent_core.base_agent.ProtegeBaseAgent` creates the shared OpenAI agent and stream. It calls your agent's input, tool, hook, and tool-output methods at the relevant points.
5. `src/main_agent.py:MainAgent` implements those agent-specific extension points. `src/financial_service.py:FinancialApiService` adds citation preparation and final citation events.

The division of responsibility is important:

- **Agent class:** behavior during an agent run, such as constructing model input, selecting extra tools, setting hooks, and processing tool output.
- **Agent factory:** model and MCP configuration plus construction of the agent class.
- **Service class:** request lifecycle behavior around the run, especially pre-run data and completion events.
- **API wiring:** chooses which factory and service handle a route.
- **Shared core:** owns the common agent execution, MCP setup, streaming, session handling, and request orchestration. Prefer its extension points over copying that machinery into `src`.

## Relevant files

| File | Role |
| --- | --- |
| `src/main_agent.py` | Current `MainAgent` implementation and examples of all four `ProtegeBaseAgent` overrides. |
| `src/agent_helper.py` | Builds the `AgentConfig` and `McpConfig`, then constructs `MainAgent`. |
| `src/financial_service.py` | Example `ApiService` subclass for request-specific data and completion handling. |
| `api.py` | Creates the service in `request_context` and uses it for `/api/chat`. |
| `protege_agent_core/base_agent.py` | Abstract agent contract and shared streamed agent execution. |
| `protege_agent_core/models.py` | `AgentConfig`, `McpConfig`, and `AgentRequest` data models. |
| `protege_agent_core/protege_base_service.py` | `ApiService` contract and chat request orchestration. |
| `src/system_prompts/` | Application prompt files used by the current agent and its specialized tools. |

## Implement a specialized agent

### 1. Decide what should be specialized

Start by identifying the behavior that differs from `MainAgent`. If the difference is only the prompt, use an existing prompt-loading pattern. If it is conditional tools, implement `get_extra_tools`. If it needs custom input or tool-output state, override the corresponding method. Add a service subclass only when behavior is needed before or after an agent run.

The `ProtegeBaseAgent` abstract methods are:

- `build_agent_input(prompt, attachments, additional_data)`: produce the input passed to the shared runner. `MainAgent` delegates common attachment handling to `_build_agent_input_default`.
- `get_extra_tools(request, event_queue)`: return tools to add to the main agent. `MainAgent` conditionally adds a financial research agent-as-tool.
- `get_hooks(agent_request)`: return the hooks for the run. `MainAgent` combines context trimming and context-size handling.
- `process_tool_output(tool_args, tool_output, additional_data)`: handle a completed tool call's output. `MainAgent` extracts citation chunk text into additional data.

The shared core builds the main model agent and invokes these methods. It also attaches its configured MCP server when `"Igz"` is in `AgentRequest.SourceMode`; `get_extra_tools` is for additional tools, not a replacement for that shared behavior.

### 2. Add the agent class

Create a class under `src` that extends `ProtegeBaseAgent`. A minimal starting point is:

```python
import asyncio
from typing import Any

from protege_agent_core.base_agent import ProtegeBaseAgent
from protege_agent_core.hooks.context_size_hook import ContextSizeHooks
from protege_agent_core.hooks.context_trimming_hook import ContextTrimmingHook
from protege_agent_core.hooks.multi_run_hooks import MultiRunHooks
from protege_agent_core.models import AgentConfig, AgentRequest, AttachToConversationRequest
from protege_agent_core.sandbox_utils import SANDBOX_ROOT_DIR
from protege_agent_core.utils import MAX_TURNS, WARN_BEFORE


class CatalogAgent(ProtegeBaseAgent):
    def __init__(self, config: AgentConfig):
        super().__init__(config)

    def build_agent_input(
        self,
        prompt: str,
        attachments: list[AttachToConversationRequest] | None = None,
        additional_data: dict[str, dict] | None = None,
    ):
        return self._build_agent_input_default(
            prompt=prompt,
            attachments=attachments,
        )

    def get_extra_tools(
        self,
        request: AgentRequest,
        event_queue: asyncio.Queue,
    ) -> list[Any]:
        return []

    def get_hooks(self, agent_request: AgentRequest):
        return MultiRunHooks(
            ContextTrimmingHook(),
            ContextSizeHooks(
                conversation_id=agent_request.ConversationId,
                user_id=agent_request.UserId,
                model=self.config.Model,
                max_turns=MAX_TURNS,
                warn_before=WARN_BEFORE,
                sandbox_working_dir=SANDBOX_ROOT_DIR,
            ),
        )

    def process_tool_output(
        self,
        tool_args: Any,
        tool_output: Any,
        additional_data: dict[str, dict] | None = None,
    ) -> None:
        return None
```

This template uses the same hook pattern as `MainAgent`. Adjust the hook configuration only when the specialized agent needs different behavior. Replace the empty tool list and no-op output handler when your agent needs those behaviors.

The default input builder handles supported attachment types. Override or extend that behavior only when the specialized agent needs a different input format; preserve any relevant document or attachment context.

### 3. Add tools when needed

`MainAgent.get_researcher_tools` is the in-repository example of a conditional sub-agent tool. It checks `request.SourceMode`, loads specialized instructions, and calls `build_generic_agent_as_tool` with the conversation, user, MCP, model, event-queue, and tracing settings.

For a specialized tool:

1. Decide how it is enabled (for example, a source-mode value).
2. Build it inside `get_extra_tools` and return it in a list.
3. Forward the relevant request context and `event_queue` so tool progress can join the main response stream.
4. Keep tool descriptions and instructions specific about scope and expected output.

The shared core separately handles its configured MCP server based on `SourceMode`; avoid opening a duplicate MCP connection unless the specialized behavior requires a separate one.

### 4. Configure and construct the agent

Add or adapt a factory following `src/agent_helper.py:create_agent`. The current factory reads the selected model configuration, constructs `McpConfig` with the request's `lna` header, creates `AgentConfig`, and instantiates `MainAgent`.

A factory for the example class would follow the same shape:

```python
from protege_agent_core.models import AgentConfig, McpConfig
from protege_agent_core.utils import (
    MCP_SERVER,
    MCP_TIMEOUT,
    get_available_models,
    get_model_config,
)
from src.catalog_agent import CatalogAgent


def create_catalog_agent(lna: str) -> CatalogAgent:
    models = get_available_models()
    model_config = get_model_config("openai", models["DefaultOpenai"])
    mcp_config = McpConfig(
        ServerName="Intelligize MCP",
        Url=MCP_SERVER,
        Timeout=MCP_TIMEOUT,
        Headers={"lna": lna},
    )
    config = AgentConfig(
        ModelApiKey=model_config["apikey"],
        ModelBaseUrl=model_config["baseurl"],
        Model=model_config["model"],
        AgentName="CatalogResearcher",
        McpConfiguration=mcp_config,
        TraceAppKey="Agent",
    )
    return CatalogAgent(config)
```

Use the application's existing secret and model-configuration helpers. Do not put credentials in the factory or source files. Keep the configured MCP headers and timeout consistent with the authentication and downstream behavior required by the request.

## Add service behavior only when necessary

`ApiService` has two abstract lifecycle methods:

- `pre_stream(existing_chat)` prepares and returns the `additional_data` dictionary supplied to `AgentRequest` and the agent.
- `on_done(event_queue, done_message)` can add completion events after the agent emits its final result.

`FinancialApiService` is an example: `pre_stream` restores citation state and returns it; `on_done` resolves citations and queues a citation event. If your agent needs no service-level customization, reuse the existing service rather than adding an empty subclass.

For custom service behavior, preserve the required methods and use `create_message_emit` to format events consistently with the existing stream:

```python
import asyncio
from typing import Any

from protege_agent_core.models import DoneMessage
from protege_agent_core.protege_base_service import ApiService


class CatalogApiService(ApiService):
    async def pre_stream(self, existing_chat: Any) -> dict[str, dict]:
        return {}

    async def on_done(
        self,
        event_queue: asyncio.Queue,
        done_message: DoneMessage,
    ) -> None:
        return None
```

Replace the placeholder methods with actual behavior, or keep using `FinancialApiService` when citation handling is still required.

## Wire the agent into the API

The current wiring is in `api.py`:

1. `request_context` obtains the request's `lna` and auth cookie.
2. It creates `FinancialApiService(create_agent(lna=lna), auth_cookie=auth_cookie)`.
3. The `/api/chat` endpoint uses the resulting service to create the stream task.

`get_lna_value` resolves the token in this order: `lna` cookie, `lna` header, `LEXIS_TOKEN` environment variable, the configured `TOKEN_KEY` header, then the `TOKEN_KEY` cookie. It rejects the request with HTTP 403 if no non-blank token is found:

```python
import os

from fastapi import HTTPException, Request, status


token_auth = os.getenv("TOKEN_KEY", "lna")


def get_lna_value(req: Request) -> str:
    lexisToken = req.cookies.get("lna", "")
    if (lexisToken or "").strip() == "":
        lexisToken = req.headers.get("lna", "")
    if (lexisToken or "").strip() == "":
        lexisToken = os.getenv("LEXIS_TOKEN", "")
    if (lexisToken or "").strip() == "":
        lexisToken = req.headers.get(token_auth, "")
    if (lexisToken or "").strip() == "":
        lexisToken = req.cookies.get(token_auth, "")

    if (lexisToken or "").strip() == "":
        raise HTTPException(status.HTTP_403_FORBIDDEN)

    return lexisToken
```

For example, to route chat through the `CatalogAgent` and `CatalogApiService` templates above, the relevant `api.py` wiring would look like this:

The logger setup attaches `CustomLogger` to OpenAI logs and adds the Sumo Logic handler when enabled. The `lifespan` context starts the publishers at application startup, then cancels active streams and closes publishers and clients during shutdown. Keep this lifecycle wiring when changing the agent factory or service.

```python
import asyncio
import logging
import os
import uvicorn
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import StreamingResponse
from protege_agent_core.api.request import ChatRequest
from protege_agent_core.custom_logger_handler import CustomLogger
from protege_agent_core.protege_base_service import ApiService
from protege_agent_core.sumologic_handler import (
    SumoLogicHandler,
    start_publisher as start_sumo_publisher,
    stop_publisher as stop_sumo_publisher,
)
from protege_agent_core.tracking_service import (
    start_publisher,
    stop_publisher,
)
from protege_agent_core.utils import close_http_client, close_openai_clients
from src.catalog_agent_helper import create_catalog_agent
from src.catalog_service import CatalogApiService


logger = CustomLogger()
sumo_handler = SumoLogicHandler()
active_stream_tasks: set[asyncio.Task] = set()

root_logger = logging.getLogger()
root_logger.setLevel(logging.ERROR)
if sumo_handler.enabled and not any(
    isinstance(handler, SumoLogicHandler) for handler in root_logger.handlers
):
    sumo_handler.setLevel(logging.ERROR)
    root_logger.addHandler(sumo_handler)

for name in ["openai"]:
    log = logging.getLogger(name)
    if not any(isinstance(handler, CustomLogger) for handler in log.handlers):
        log.handlers.clear()
        log.setLevel(logging.DEBUG)
        log.addHandler(logger)
        log.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_publisher()
    start_sumo_publisher()
    try:
        yield
    finally:
        tasks = list(active_stream_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(stop_publisher)
        await asyncio.to_thread(stop_sumo_publisher)
        await close_http_client()
        await close_openai_clients()


env = os.getenv("ENV", "dev")
if env.lower() != "prod":
    app = FastAPI(lifespan=lifespan)
else:
    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )


def create_stream_task(
    service: ApiService,
    request: ChatRequest,
    queue: asyncio.Queue,
) -> asyncio.Task:
    task = asyncio.create_task(service.run_stream(request=request, queue=queue))
    active_stream_tasks.add(task)
    task.add_done_callback(active_stream_tasks.discard)
    return task


async def request_context(req: Request):
    lna = get_lna_value(req)
    auth_cookie = get_auth_cookie_value(req=req)
    service = CatalogApiService(create_catalog_agent(lna=lna))

    req.state.lna = lna
    req.state.AuthCookie = auth_cookie
    req.state.service = service
    user_id = await service.get_user_id(lna, remove_colon=False)
    req.state.user_id = user_id
    service.set_user_id(user_id=user_id)
    return req.state


async def run_stream(
    queue: asyncio.Queue,
    service: ApiService,
    task: asyncio.Task,
    stop_task_on_cancel: bool = False,
):
    while True:
        try:
            data = await queue.get()
            if data is not None:
                if isinstance(data, dict) and "dismiss_key" in data:
                    service.internal_dismiss(data["dismiss_key"])
                elif data == "|*DONE*|":
                    break
                else:
                    yield data
        finally:
            if stop_task_on_cancel and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@app.post("/api/chat")
async def chat(
    request: ChatRequest,
    req: Request,
    ctx=Depends(request_context),
):
    service = ctx.service
    request.LexisToken = ctx.lna
    if (request.AuthCookie or "").strip() == "":
        request.AuthCookie = req.state.AuthCookie

    queue = asyncio.Queue()
    task = create_stream_task(service, request, queue)
    return StreamingResponse(
        run_stream(queue=queue, service=service, task=task),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    uvicorn.run("api:app", host="0.0.0.0", port=8001, workers=1)
```

## Build and run the API image

`Dockerfile.api` packages the FastAPI application. It installs dependencies from `requirements.txt`, copies `api.py` and the complete `src` directory, creates the writable conversation/session/agent-data directories, and runs Uvicorn as the unprivileged `app` user on port 8000. New Python modules under `src` are included automatically; add any new third-party runtime dependency to `requirements.txt`.

The shared `protege_agent_core` package is also imported by the API, but the current `Dockerfile.api` does not copy or install this local package. Before relying on the image, make the package available in the image, for example by adding `COPY protege_agent_core ./protege_agent_core` alongside the existing source copies. If runtime code needs files outside `src`, add explicit `COPY` instructions for them and ensure the `app` user can read them. Do not bake credentials or populated secret files into the image; provide them through the runtime environment or mounted files.

Build and run the image from the repository root:

```powershell
docker build -f Dockerfile.api -t ai-agent-api .
docker run --rm -p 8000:8000 --env-file .env ai-agent-api
```

Provide the required AWS identity and network access to `MCP_SERVER` and downstream services at runtime. Keep `.env` out of source control.

Run the module from the repository root with `python api.py` to start the local API on port `8001`. Keep this module entry point when wiring a different agent so the service remains directly runnable with Uvicorn. This illustrates the application-level wiring; keep the existing error reporting and stream-task lifecycle handling in `api.py` when making the real change. Since the current `create_stream_task` and `run_stream` helpers annotate their service argument as `FinancialApiService`, change those annotations to the shared `ApiService` base if they will accept multiple service implementations.

If the route needs different behavior, choose the service deliberately: retain `FinancialApiService` if it still provides required citation handling, or use a corresponding `ApiService` subclass. Keep user lookup, service initialization, and request authentication in the same request lifecycle. Do not create a second independent streaming path when the existing service can be reused.

## Prompts and source modes

The current `MainAgent` adds financial research only when `"financialresearch"` appears in `request.SourceMode`. The shared base also attaches the configured MCP server only when `"Igz"` is present, and handles web search when `"Web"` is present. Use the existing request contract and source-mode conventions when adding conditional capabilities; ensure the caller can actually supply the value before relying on it.

Prompt files currently live in `src/system_prompts/`. `MainAgent` loads its financial tool instructions through `read_financial_prompt` in `src/utils.py`. Follow the existing prompt-loading pattern and keep prompt files separate from Python implementation where appropriate.

## Validate the integration

Before considering the agent complete:

1. Confirm the new class implements all four abstract methods with compatible inputs and expected return values.
2. Confirm its factory supplies the required model and MCP configuration without embedding secrets.
3. Confirm API wiring constructs the intended agent and service for the correct route.
4. Exercise a chat request with the relevant source modes and attachments, then verify streamed events, tool execution, and any completion events.
5. Exercise a follow-up conversation if the agent stores state in `additional_data` or relies on restored conversation data.

For shared core behavior and exact contracts, consult `protege_agent_core/base_agent.py`, `protege_agent_core/models.py`, and `protege_agent_core/protege_base_service.py`; those are the runtime interfaces the application implementation must satisfy.
