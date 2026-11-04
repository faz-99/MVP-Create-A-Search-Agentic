"""OpenAI loop end to end with a scripted Agents SDK model and the live MCP server.

The fake model replays: a lookup, a plain-text reply with no submit (must trigger a
reminder), then a valid submit (must end the run via tool_use_behavior). Needs
LEXIS_SSO_COOKIE.

    python -m tests.test_openai_loop_scripted
"""

import asyncio
import json
import uuid

from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from create_search.loops import openai_loop
from create_search.run import create_search

PAYLOAD = {"companyCik": ["0000320193"], "formTypes": [6]}


def _call(name, args):
    return ResponseFunctionToolCall(
        id=f"fc_{uuid.uuid4().hex[:8]}", call_id=f"call_{uuid.uuid4().hex[:8]}",
        name=name, arguments=json.dumps(args), type="function_call",
    )


def _text(text):
    return ResponseOutputMessage(
        id=f"msg_{uuid.uuid4().hex[:8]}", role="assistant", status="completed", type="message",
        content=[ResponseOutputText(text=text, type="output_text", annotations=[])],
    )


TURNS = [
    [_call("company_match", {"company_query": "Apple"})],
    [_text("Here is your search: Apple 8-Ks.")],
    [_call("submit_search", {
        "strategy": "Apple 8-K filings.", "tool": "sec_filings_search",
        "payload": PAYLOAD, "analysis_instructions": "Summarise each event.",
    })],
]


class FakeModel(Model):
    def __init__(self):
        self.inputs = []

    async def get_response(self, *args, **kwargs):
        raise NotImplementedError

    async def stream_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs, tracing, **kwargs):
        self.inputs.append(input)
        output = TURNS[len(self.inputs) - 1]
        response = Response.model_construct(
            id=f"resp_{uuid.uuid4().hex[:8]}", created_at=0, model="fake", object="response",
            output=output, parallel_tool_calls=True, tool_choice="auto", tools=[],
            usage=None,
        )
        yield ResponseCompletedEvent.model_construct(type="response.completed", response=response, sequence_number=0)


async def main() -> None:
    fake = FakeModel()
    openai_loop.get_openai_model = lambda *a, **k: fake
    openai_loop.openai_config = lambda model: {"model": "gpt-5-fake", "apikey": "x", "baseurl": None}

    events = [ev async for ev in create_search("8-Ks of Apple", provider="openai")]
    kinds = [e["kind"] for e in events]
    done = events[-1]
    assert done["kind"] == "done", (kinds, events[-1])
    result = done["message"]
    assert result["runnable"] and result["source"] == "agent" and result["payload"] == PAYLOAD, result
    assert len(fake.inputs) == 3, len(fake.inputs)
    assert any(e["kind"] == "status" and "reminding" in e["message"] for e in events), kinds
    print("kinds:", kinds)
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
