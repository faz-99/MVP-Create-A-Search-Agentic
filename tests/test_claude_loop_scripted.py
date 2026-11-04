"""Claude loop end to end with a scripted model (no Anthropic call) and the live MCP server.

The fake client replays three turns: thinking + a lookup, an invalid submit, a valid
submit. Checks thinking blocks are passed back, cache_control is set, and the run ends
on the accepted submit. Needs LEXIS_SSO_COOKIE.

    python -m tests.test_claude_loop_scripted
"""

import asyncio
import copy
from types import SimpleNamespace as NS

from create_search.loops import claude_loop
from create_search.run import create_search

PAYLOAD = {"companyCik": ["0000320193"], "formTypes": [6], "dateFrom": "01/01/2026"}
SUBMIT = {
    "strategy": "Apple 8-K filings from 2026.",
    "tool": "sec_filings_search",
    "analysis_instructions": "Summarise each 8-K's event.",
}


def _usage():
    return NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=50, cache_creation_input_tokens=0)


def _tool(id_, name, input_):
    return NS(type="tool_use", id=id_, name=name, input=input_)


TURNS = [
    [NS(type="thinking", thinking="Resolve Apple first.", signature="sig1"), _tool("t1", "company_match", {"company_query": "Apple"})],
    [_tool("t2", "submit_search", {**SUBMIT, "payload": {"bogus": 1}})],
    [_tool("t3", "submit_search", {**SUBMIT, "payload": PAYLOAD})],
]


class FakeMessages:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))  # the loop keeps mutating its list
        content = TURNS[len(self.calls) - 1]
        return NS(content=content, usage=_usage(), stop_reason="tool_use")


async def main() -> None:
    fake = FakeMessages()
    claude_loop.get_claude_client = lambda: NS(messages=fake)

    events = [ev async for ev in create_search("8-Ks of Apple filed this year", provider="claude")]
    kinds = [e["kind"] for e in events]
    done = events[-1]
    assert done["kind"] == "done", kinds
    result = done["message"]
    assert result["runnable"] and result["source"] == "agent" and result["payload"] == PAYLOAD, result
    assert len(fake.calls) == 3, len(fake.calls)

    first = fake.calls[0]
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert first["tools"][-1]["name"] == "submit_search" and "cache_control" in first["tools"][-1]
    second_msgs = fake.calls[1]["messages"]
    assert second_msgs[1]["content"][0] == {"type": "thinking", "thinking": "Resolve Apple first.", "signature": "sig1"}
    third_msgs = fake.calls[2]["messages"]
    assert third_msgs[-1]["content"][0]["is_error"] is True  # rejected submit went back as an error
    print("kinds:", kinds)
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
