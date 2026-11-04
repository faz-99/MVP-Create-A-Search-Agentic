"""MCP connection, set up as agentv4 does.

Both loops use the Agents SDK's MCPServerStreamableHttp purely as an MCP client: the
connection runs in our process, so the custom `lna` auth header is no obstacle (hosted
provider connectors can only send Authorization, which is why the POC had to hand-roll
its own client). The SDK retries failed calls with exponential backoff, which covers the
503s from unhealthy pods behind the ELB.

One pooled HTTP/2 transport is shared by every request; per-request clients reuse it and
must not close it.
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from agents.mcp import MCPServerStreamableHttp

from create_search import config


class _SharedTransport(httpx.AsyncHTTPTransport):
    async def __aexit__(self, *args) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def close_pool(self) -> None:
        await super().aclose()


_transport = _SharedTransport(
    http2=True,
    limits=httpx.Limits(max_connections=300, max_keepalive_connections=50, keepalive_expiry=120),
)


def _pooled_client(headers=None, timeout=None, auth=None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=_transport, headers=headers, timeout=timeout, auth=auth, follow_redirects=False
    )


@asynccontextmanager
async def open_mcp(token: str, conversation_id: str = "") -> AsyncIterator[MCPServerStreamableHttp]:
    headers = {config.AUTH_HEADER: token}
    if conversation_id:
        headers["X-Conversation"] = conversation_id
    async with MCPServerStreamableHttp(
        name=config.MCP_SERVER_NAME,
        params={
            "url": config.MCP_SERVER_URL,
            "headers": headers,
            "timeout": config.MCP_TIMEOUT,
            "httpx_client_factory": _pooled_client,
        },
        client_session_timeout_seconds=config.MCP_TIMEOUT,
        cache_tools_list=True,
        max_retry_attempts=config.MCP_RETRY_ATTEMPTS,
        retry_backoff_seconds_base=0.5,
    ) as server:
        # Let parallel lookups share the session instead of queueing behind each other.
        server._serialize_session_requests = False
        yield server


async def list_tool_dicts(server: MCPServerStreamableHttp) -> list[dict]:
    """The server's tools as plain dicts: name, description, inputSchema."""
    tools = await server.list_tools()
    return [
        {"name": t.name, "description": t.description or "", "inputSchema": t.inputSchema or {}}
        for t in tools
    ]


async def close_transport() -> None:
    await _transport.close_pool()
