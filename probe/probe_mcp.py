"""Enumerate the DDC4 MCP server's tool surface.

Run this first. Nothing else can be designed sensibly until we know what tools/list
returns — the names and input schemas are what the agent composes against, and the
execute-tool name in shared/toolsets.py has to match one of them or the build-phase
gate is not actually holding.

Run from the project root:

    python -m probe.probe_mcp            # summary: one line per tool
    python -m probe.probe_mcp --full     # full JSON, including input schemas
"""

import json
import sys

from shared.mcp_client import MCPClient, MCPError
from shared.toolsets import (
    EXECUTE_TOOLS,
    available_execute_tools,
    resolve_execute_tool,
    unclassified_search_tools,
)


def main() -> int:
    try:
        with MCPClient() as mcp:
            info = mcp.server_info
            print(
                f"Connected to {info.get('name', '?')} v{info.get('version', '?')}\n"
            )
            tools = mcp.list_tools()
    except MCPError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if "--full" in sys.argv:
        print(json.dumps(tools, indent=2))
        return 0

    print(f"{len(tools)} tools:\n")
    for tool in sorted(tools, key=lambda t: t.get("name", "")):
        params = ", ".join(tool.get("inputSchema", {}).get("properties", {}))
        description = (tool.get("description") or "").split("\n")[0]
        print(f"  {tool.get('name')}({params})")
        if description:
            print(f"      {description}")

    present = available_execute_tools(tools)
    print()
    if present:
        print(f"Withheld from the build phase ({len(present)}):")
        for name in present:
            print(f"  - {name}")
    else:
        print(
            "WARNING: no tool matched EXECUTE_TOOLS in shared/toolsets.py\n"
            f"  currently: {', '.join(sorted(EXECUTE_TOOLS))}\n"
            "  Until this matches a real tool name, the build phase is not gated and\n"
            "  the Run button has nothing to call. Update EXECUTE_TOOLS."
        )

    drift = unclassified_search_tools(tools)
    if drift:
        print(
            "\nGATE WARNING: these look like search tools but are classified as "
            "neither executor nor lookup:"
        )
        for name in drift:
            print(f"  - {name}")
        print(
            "  Classify them in shared/toolsets.py — until then the build phase can\n"
            "  call them, which defeats the gate."
        )
    else:
        print("\nGate: every search-looking tool is classified.")

    print("\nRun with --full for the complete input schemas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
