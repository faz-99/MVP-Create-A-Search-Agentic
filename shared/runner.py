"""Phase 2 — execute a created search.

Deliberately model-free. The agent chose the tool and built the arguments; running
them is a deterministic MCP tool call. Shared by the UI's Run button and the CLI's
--run flag so both go through the same check.
"""

from dataclasses import dataclass
from typing import Any

from shared.mcp_client import MCPClient, MCPError
from shared.toolsets import is_execute_tool
from shared.validate import validate_payload


@dataclass
class RunOutcome:
    ok: bool
    tool: str | None = None
    result: Any = None
    error: str | None = None


def execute_search(tool: str, payload: dict[str, Any]) -> RunOutcome:
    """Call the named search tool with `payload`.

    The tool name comes from the model, so it is validated against EXECUTE_TOOLS
    before anything is called. That check is the gate: without it, a hallucinated or
    substituted tool name would let this path invoke an arbitrary tool on the server,
    which is exactly what withholding the executors during the build phase prevents.
    """
    if not tool:
        return RunOutcome(
            ok=False, error="No tool named in the created search — nothing to run."
        )
    if not is_execute_tool(tool):
        return RunOutcome(
            ok=False,
            tool=tool,
            error=(
                f"'{tool}' is not a known search tool, so it will not be called. "
                "Either the model named something that does not exist, or "
                "EXECUTE_TOOLS in shared/toolsets.py is out of date."
            ),
        )

    try:
        with MCPClient() as mcp:
            # Last guard: the payload is operator-editable in the UI, so validate the
            # value actually being sent rather than trusting the build-time check.
            errors = validate_payload(tool, payload, mcp.list_tools())
            if errors:
                return RunOutcome(
                    ok=False,
                    tool=tool,
                    error="Payload does not match the tool schema: " + "; ".join(errors[:4]),
                )
            result = mcp.call_tool(tool, payload)
    except MCPError as exc:
        return RunOutcome(ok=False, tool=tool, error=str(exc))

    # A tool that fails its own work returns normally with isError — that is a result
    # to report, not a transport failure.
    is_error = bool(result.get("isError", False))
    return RunOutcome(
        ok=not is_error,
        tool=tool,
        result=result,
        error=_classify_error(tool, result) if is_error else None,
    )


def _classify_error(tool: str, result: dict[str, Any]) -> str:
    """Name the failure class, because the fix differs sharply between them.

    The server advertises all 63 tools regardless of what the credential is licensed
    for, so an unentitled dataset fails only at run time with a 403. That is not a
    payload problem and no amount of rebuilding the search will fix it — telling the
    operator otherwise sends them down the wrong path.
    """
    body = " ".join(
        block.get("text", "")
        for block in result.get("content", [])
        if block.get("type") == "text"
    )

    if "403" in body or "Forbidden" in body:
        return (
            f"'{tool}' returned 403 Forbidden. The credential is not entitled to this "
            "dataset — the tool exists but this user cannot query it. Rebuilding the "
            "search will not help; use a different dataset or a credential with the "
            f"entitlement. Server said: {body[:200]}"
        )
    if "validation error" in body.lower():
        return (
            f"'{tool}' rejected the arguments. Filter values are usually resolved ids "
            f"rather than readable names — check the resolver tools. Server said: "
            f"{body[:300]}"
        )
    return f"'{tool}' reported an error: {body[:300]}" if body else "Tool reported an error."
