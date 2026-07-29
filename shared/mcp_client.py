"""Minimal MCP client over streamable HTTP for the DDC4 server.

Needed in three places:

  - the probe, to enumerate the tool surface;
  - the OpenAI agent, to compute an allowlist (`allowed_tools` is allow-only, so the
    execute tool has to be excluded by name from the full list);
  - the Run button, which calls the execute tool directly with the payload the agent
    produced. That path is deterministic and involves no model.
"""

import json
import time
from typing import Any

import httpx

from shared.config import AUTH_HEADER_NAME, MCP_SERVER_URL, mcp_token

PROTOCOL_VERSION = "2025-06-18"

# Unhealthy pods behind the ELB return 503; retrying lands on a different backend.
RETRY_ATTEMPTS = 6
RETRY_BACKOFF = 0.6


class MCPError(RuntimeError):
    pass


class MCPClient:
    """One initialized MCP session. Use as a context manager."""

    def __init__(self, url: str = MCP_SERVER_URL, token: str | None = None):
        self._url = url
        self._token = token or mcp_token()
        self._client: httpx.Client | None = None
        self._next_id = 0
        # Auth is the LexisObSSOCookie value in a custom `lna` header. Sending it
        # as `Authorization: Bearer` is rejected with 401 invalid_token.
        self._headers = {
            AUTH_HEADER_NAME: self._token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        self.server_info: dict[str, Any] = {}

    def __enter__(self) -> "MCPClient":
        self._client = httpx.Client(timeout=120.0)
        self._initialize()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._client:
            self._client.close()
            self._client = None

    # -- protocol ---------------------------------------------------------------

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        """POST with retry on 503.

        The host resolves to several pods behind a Kubernetes ELB and some of them
        return 503 while others serve fine, so a single 503 says nothing about
        whether the service is up — only that this connection landed on a bad
        backend. Retrying re-resolves and usually lands elsewhere. Without this,
        requests fail at random.
        """
        assert self._client is not None, "use MCPClient as a context manager"
        last: httpx.Response | None = None
        last_exc: Exception | None = None

        for attempt in range(RETRY_ATTEMPTS):
            try:
                last = self._client.post(self._url, headers=self._headers, json=body)
                last_exc = None
                if last.status_code != 503:
                    return last
            except httpx.TransportError as exc:
                # DNS blips and connection resets are as transient as a 503 here, and
                # they must not escape as raw httpx errors: callers only catch
                # MCPError, so an httpx.ConnectError reaching them produces an empty
                # 500 with no explanation.
                last_exc = exc

            if attempt < RETRY_ATTEMPTS - 1:
                time.sleep(RETRY_BACKOFF * (attempt + 1))

        if last_exc is not None:
            raise MCPError(
                f"Could not reach the MCP server after {RETRY_ATTEMPTS} attempts: "
                f"{type(last_exc).__name__}: {last_exc}"
            ) from last_exc
        assert last is not None
        return last

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            body["params"] = params

        response = self._post(body)
        message = _parse(response)
        if "error" in message:
            error = message["error"]
            raise MCPError(f"{method} failed: {error.get('message', error)}")
        return message.get("result", {})

    def _initialize(self) -> None:
        self._next_id += 1
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "ddc4-agent", "version": "0.1.0"},
                },
            }
        )
        message = _parse(response)
        if "error" in message:
            raise MCPError(f"initialize failed: {message['error']}")

        # The session id arrives as a header and must ride on every later request.
        session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            self._headers["Mcp-Session-Id"] = session_id

        self.server_info = message.get("result", {}).get("serverInfo", {})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    # -- api --------------------------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        return self._rpc("tools/list").get("tools", [])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool. Raises MCPError on transport/protocol failure.

        A tool that fails *its own* work returns normally with `isError: true` — that
        is a result to report, not an exception, so it is left for the caller.
        """
        return self._rpc("tools/call", {"name": name, "arguments": arguments})


def _parse(response: httpx.Response) -> dict[str, Any]:
    """Streamable HTTP replies with either JSON or a single SSE event."""
    if response.status_code == 401:
        raise MCPError(
            "MCP server rejected the credential (401). Check LEXIS_SSO_COOKIE in "
            ".env — these cookies expire, so it may just need refreshing."
        )
    if response.status_code == 503:
        raise MCPError(
            f"MCP server returned 503 after {RETRY_ATTEMPTS} attempts. Every backend "
            "in the pool is unhealthy — this is a server-side problem, not auth."
        )
    response.raise_for_status()
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise MCPError(f"No data frame in SSE response:\n{response.text}")
    return json.loads(response.text)
