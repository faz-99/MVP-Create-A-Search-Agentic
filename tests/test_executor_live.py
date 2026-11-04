"""Live check of ToolExecutor against the MCP server, with no model involved.

Plays the calls a model would make for "8-Ks of Apple": resolve the company and the form
type, run the search, submit a bad payload, then a good one. Needs LEXIS_SSO_COOKIE.

    python -m tests.test_executor_live
"""

import asyncio
import json

from create_search import config
from create_search.core.executor import ToolExecutor
from create_search.core.recorder import RunRecorder
from create_search.core.toolset import select_tools
from create_search.mcp_connection import list_tool_dicts, open_mcp


async def main() -> None:
    events: list[dict] = []
    async with open_mcp(config.dev_token()) as server:
        toolset = select_tools(await list_tool_dicts(server), ["SF", "AOE"])
        ex = ToolExecutor(server, toolset, events.append, RunRecorder("test", "none", "8-Ks of Apple", ["SF", "AOE"]))

        names = [d["name"] for d in ex.tool_defs()]
        assert names[-2:] == ["report_strategy", "submit_search"], names[-2:]

        text, err = await ex.execute("report_strategy", {"strategy": "Apple 8-K filings."})
        assert not err and any(e["kind"] == "strategy" for e in events)

        text, err = await ex.execute("company_match", {"company_query": "Apple Inc"})
        assert not err and "0000320193" in text, text[:300]

        text, err = await ex.execute("sec_filings_find_form_types", {"query": "8-K"})
        assert err and "Arguments rejected" in text, text[:300]

        text, err = await ex.execute("sec_filings_find_form_types", {"formTypes": ["8-K"]})
        assert not err, text[:300]
        print("form types:", text[:300])

        text, err = await ex.execute("sec_filings_get_full_content", {"documentId": "x"})
        assert err and "not available" in text

        payload = {"companyCik": ["0000320193"], "pageSize": 50, "includeExhibits": False}
        text, err = await ex.execute("sec_filings_search", payload)
        assert not err and ex.search_runs == 1, text[:300]
        assert json.loads(text.split("\n", 1)[1])["rows_returned"] <= config.SEARCH_PREVIEW_ROWS
        print("search preview:", text[:400])

        text, err = await ex.execute("sec_filings_search", payload)
        assert "Identical" in text and ex.search_runs == 1

        text, err = await ex.execute("sec_filings_search", {"formTypeIds": [1]})
        assert err and "unknown field" in text and ex.search_runs == 1, text

        text, err = await ex.execute(
            "submit_search",
            {"strategy": "s", "tool": "sec_filings_search", "payload": {"query": "x"}, "analysis_instructions": "a"},
        )
        assert err and ex.submitted is None, text

        assert ex.result("test").source == "fallback"

        text, err = await ex.execute(
            "submit_search",
            {"strategy": "Apple filings.", "tool": "sec_filings_search", "payload": payload, "analysis_instructions": "a"},
        )
        assert not err and ex.submitted is not None and ex.result("").runnable, text

    print("events:", [e["kind"] for e in events])
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
