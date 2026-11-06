"""MCP client as agentv4: MCPServerStreamableHttp on one pooled HTTP/2 transport, lna auth header."""

from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from agents.mcp import MCPServerStreamableHttp

from create_search import config


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


async def close_transport() -> None:
    await _transport.close_pool()
