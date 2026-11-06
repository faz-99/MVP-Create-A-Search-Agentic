"""External connections: the MCP server and the model clients.

MCP as agentv4: MCPServerStreamableHttp on one pooled HTTP/2 transport, lna auth header.
OpenAI keys from Secrets Manager (MODELS_SECRETS, as agentv4) or OPENAI_API_KEY locally; Claude via the Anthropic SDK.
"""

import json
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import anthropic
import httpx
from agents import OpenAIResponsesModel
from agents.mcp import MCPServerStreamableHttp
from openai import AsyncOpenAI

from create_search import config


# --- MCP ---------------------------------------------------------------------------------------------------------
class _SharedTransport(httpx.AsyncHTTPTransport):
    """Shared by all requests; per-request clients must not close it."""

    async def __aexit__(self, *args) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def close_pool(self) -> None:
        await super().aclose()


_transport = _SharedTransport(http2=True, limits=httpx.Limits(max_connections=300, max_keepalive_connections=50, keepalive_expiry=120))


def _pooled_client(headers=None, timeout=None, auth=None) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_transport, headers=headers, timeout=timeout, auth=auth, follow_redirects=False)


@asynccontextmanager
async def open_mcp(token: str, conversation_id: str = "") -> AsyncIterator[MCPServerStreamableHttp]:
    headers = {config.AUTH_HEADER: token, **({"X-Conversation": conversation_id} if conversation_id else {})}
    params = {"url": config.MCP_SERVER_URL, "headers": headers, "timeout": config.MCP_TIMEOUT, "httpx_client_factory": _pooled_client}
    async with MCPServerStreamableHttp(name=config.MCP_SERVER_NAME, params=params, client_session_timeout_seconds=config.MCP_TIMEOUT,
                                       cache_tools_list=True, max_retry_attempts=config.MCP_RETRY_ATTEMPTS,
                                       retry_backoff_seconds_base=0.5) as server:
        server._serialize_session_requests = False  # parallel lookups share the session
        yield server


async def list_tool_dicts(server: MCPServerStreamableHttp) -> list[dict]:
    return [{"name": t.name, "description": t.description or "", "inputSchema": t.inputSchema or {}} for t in await server.list_tools()]


# --- Models ------------------------------------------------------------------------------------------------------
_secrets: dict[str, Any] | None = None
_openai_clients: dict[tuple[str, str | None], AsyncOpenAI] = {}
_claude_client = None


def _openai_secrets() -> dict[str, Any]:
    """{key: {Model, ApiKey, RequestUri}} for the open_ai_* entries; {} without MODELS_SECRETS."""
    global _secrets
    if _secrets is None:
        _secrets = {}
        if name := os.getenv("MODELS_SECRETS"):
            import boto3
            client = boto3.session.Session().client("secretsmanager", region_name=config.AWS_REGION)
            raw = json.loads(client.get_secret_value(SecretId=name)["SecretString"])
            _secrets = {k: json.loads(v)[0] for k, v in raw.items() if "open_ai" in k}
    return _secrets


def openai_config(model: str | None) -> dict[str, str | None]:
    """{"model", "apikey", "baseurl"} for a secrets key or a plain model name."""
    key = model or os.getenv("DEFAULT_OPENAI_MODEL_KEY", "")
    if values := _openai_secrets().get(key):
        return {"model": values["Model"], "apikey": values["ApiKey"], "baseurl": values["RequestUri"].split("v1", 1)[0] + "/v1"}
    if not (api_key := os.getenv("OPENAI_API_KEY")):
        raise RuntimeError("No OpenAI credentials: set MODELS_SECRETS (server) or OPENAI_API_KEY (local).")
    return {"model": model or config.DEFAULT_OPENAI_MODEL, "apikey": api_key, "baseurl": os.getenv("OPENAI_BASE_URL") or None}


def get_openai_model(api_key: str, base_url: str | None, model_name: str) -> OpenAIResponsesModel:
    """One AsyncOpenAI client per (key, url), reused across requests."""
    client = _openai_clients.get((api_key, base_url))
    if client is None:
        client = _openai_clients[(api_key, base_url)] = AsyncOpenAI(api_key=api_key, base_url=base_url)
    return OpenAIResponsesModel(model=model_name, openai_client=client)


def get_claude_client():
    global _claude_client
    if _claude_client is None:
        if config.CLAUDE_BACKEND == "anthropic":
            _claude_client = anthropic.AsyncAnthropic()
        elif config.CLAUDE_BACKEND == "bedrock-invoke":
            _claude_client = anthropic.AsyncAnthropicBedrock(aws_region=config.AWS_REGION)
        else:
            _claude_client = anthropic.AsyncAnthropicBedrockMantle(aws_region=config.AWS_REGION)
    return _claude_client


def available_models() -> dict[str, Any]:
    keys = sorted(_openai_secrets())
    return {
        "openai": keys or [config.DEFAULT_OPENAI_MODEL],
        "claude": [config.DEFAULT_CLAUDE_MODEL],
        "DefaultOpenai": os.getenv("DEFAULT_OPENAI_MODEL_KEY") or (keys[0] if keys else config.DEFAULT_OPENAI_MODEL),
        "DefaultClaude": config.DEFAULT_CLAUDE_MODEL,
    }


async def close_clients() -> None:
    """Close model clients and the shared MCP transport (app shutdown)."""
    global _claude_client
    for client in _openai_clients.values():
        await client.close()
    _openai_clients.clear()
    if _claude_client is not None:
        await _claude_client.close()
        _claude_client = None
    await _transport.close_pool()
