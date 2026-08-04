"""Model-call seam for the client-side tool loop.

The tool loop is provider-shaped, not gateway-shaped: it needs exactly one
capability — "given messages and tool schemas, return either tool calls or text".
Everything else (which MCP tools exist, how the payload is captured, how the gate
holds) is already provider-independent.

So the loop takes an `LLM` and does not care what is behind it:

  BedrockLLM     Bedrock Converse, AWS credentials, native tool calling
  OpenAILLM      OpenAI SDK against an OpenAI-compatible endpoint
  GatewayLLM     Intelligize gateway, endpoint_secret_key + account_type

The gateway keys (aws_bedrock_claude_4_sonnet, open_ai_gpt_5_2_2025_12_11) are not
vendor model ids and cannot be handed to a vendor SDK, so `GatewayLLM` speaks to the
gateway's own HTTP endpoint. The open question was whether it could drive the loop at
all, since the loop needs structured tool calls back and the gateway is text-in,
text-out: it can, by carrying tool calls as a prompt-level JSON protocol. See
`GatewayLLM` for what that costs (no token usage, compliance not enforced).
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Completion:
    """One assistant turn: text, plus any tool calls it wants run."""

    text: str = ""
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    raw: Any = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLM(Protocol):
    """What the tool loop needs. Deliberately one method."""

    name: str

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Completion:
        """Return the next assistant turn.

        `tools` are MCP tool definitions ({name, description, inputSchema}); each
        adapter translates them into its own provider's schema shape. `messages` is
        the loop's own neutral history — adapters translate on the way in and record
        provider-native turns on the way out.
        """
        ...


class BedrockLLM:
    """Claude on Bedrock via boto3 `converse`. Verified working for tool calling.

    Uses AWS credentials, so it needs no vendor API key and no gateway. Note the
    model id must be an inference-profile id (`us.` prefix) — see config.
    """

    def __init__(
        self,
        model_id: str | None = None,
        profile: str | None = None,
        region: str | None = None,
    ):
        from shared.config import BEDROCK_MODEL_ID, BEDROCK_PROFILE, BEDROCK_REGION

        self.model_id = model_id or BEDROCK_MODEL_ID
        self._profile = profile or BEDROCK_PROFILE
        self._region = region or BEDROCK_REGION
        self.name = f"bedrock:{self.model_id}"
        self._client = None

    def _runtime(self):
        if self._client is None:
            import boto3

            # Only pass profile_name when one is actually configured. Passing None
            # (or an unset profile name) makes boto3 raise ProfileNotFound instead of
            # falling back to its credential chain, which is how the instance role on
            # the deploy host gets used.
            kwargs = {"region_name": self._region}
            if self._profile:
                kwargs["profile_name"] = self._profile
            session = boto3.Session(**kwargs)
            self._client = session.client("bedrock-runtime")
        return self._client

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Completion:
        response = self._runtime().converse(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=[_to_converse(m) for m in messages],
            toolConfig={
                "tools": [
                    {
                        "toolSpec": {
                            "name": t["name"],
                            "description": t.get("description") or t["name"],
                            "inputSchema": {"json": t.get("inputSchema") or {}},
                        }
                    }
                    for t in tools
                ]
            },
            # The plan block is the last thing the model writes, so truncation costs
            # exactly the deliverable — headroom is cheap insurance.
            inferenceConfig={"maxTokens": 16384},
        )

        blocks = response["output"]["message"]["content"]
        return Completion(
            text="".join(b["text"] for b in blocks if "text" in b),
            thinking="".join(
                b["reasoningContent"]["reasoningText"]["text"]
                for b in blocks
                if "reasoningContent" in b
            ),
            tool_calls=[
                ToolCall(
                    id=b["toolUse"]["toolUseId"],
                    name=b["toolUse"]["name"],
                    arguments=b["toolUse"]["input"],
                )
                for b in blocks
                if "toolUse" in b
            ],
            stop_reason=response.get("stopReason"),
            input_tokens=response.get("usage", {}).get("inputTokens", 0),
            output_tokens=response.get("usage", {}).get("outputTokens", 0),
            raw=response,
        )


def _to_converse(message: dict[str, Any]) -> dict[str, Any]:
    """Neutral loop history -> Converse message shape."""
    content: list[dict[str, Any]] = []
    for block in message["content"]:
        kind = block.get("type")
        if kind == "text":
            content.append({"text": block["text"]})
        elif kind == "tool_use":
            content.append(
                {
                    "toolUse": {
                        "toolUseId": block["id"],
                        "name": block["name"],
                        "input": block["input"],
                    }
                }
            )
        elif kind == "tool_result":
            content.append(
                {
                    "toolResult": {
                        "toolUseId": block["tool_use_id"],
                        "content": [{"text": block["content"]}],
                        **({"status": "error"} if block.get("is_error") else {}),
                    }
                }
            )
    return {"role": message["role"], "content": content}


class OpenAILLM:
    """OpenAI SDK against an OpenAI-compatible endpoint (AWS-hosted on the server).

    Uses **chat.completions**, not the Responses API: AWS's OpenAI-compatible surface
    exposes /chat/completions, and the Responses API is not part of it. Auth is a
    bearer token, so the SDK needs no AWS-specific code — only base_url and a key.

    Expected to be unavailable locally. `available()` reports that without raising so
    callers can fall back to the Bedrock Converse path in dev and use this once
    deployed.
    """

    def __init__(self, model: str | None = None, base_url: str | None = None):
        from shared.config import OPENAI_MODEL, openai_credentials

        creds = openai_credentials()
        self._base_url = base_url or (creds[0] if creds else None)
        self._api_key = creds[1] if creds else None
        self.model = model or OPENAI_MODEL
        self.name = f"openai-sdk:{self.model}"
        self._client = None

    def available(self) -> bool:
        return bool(self._api_key and self._base_url)

    def _openai(self):
        if self._client is None:
            if not self.available():
                raise RuntimeError(
                    "No OpenAI-compatible endpoint credentials. Set "
                    "AWS_BEARER_TOKEN_BEDROCK (or OPENAI_API_KEY) and optionally "
                    "OPENAI_BASE_URL. This path is expected to work on the deploy "
                    "target, not locally."
                )
            from openai import OpenAI

            self._client = OpenAI(base_url=self._base_url, api_key=self._api_key)
        return self._client

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Completion:
        payload = [{"role": "system", "content": system}]
        for message in messages:
            payload.extend(_to_chat(message))

        response = self._openai().chat.completions.create(
            model=self.model,
            messages=payload,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description") or t["name"],
                        "parameters": t.get("inputSchema") or {},
                    },
                }
                for t in tools
            ],
        )

        choice = response.choices[0]
        calls: list[ToolCall] = []
        for call in choice.message.tool_calls or []:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(id=call.id, name=call.function.name, arguments=args))

        usage = response.usage
        return Completion(
            text=choice.message.content or "",
            tool_calls=calls,
            stop_reason=choice.finish_reason,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            raw=response,
        )


def _to_chat(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Neutral loop history -> Chat Completions messages.

    Chat Completions splits a turn differently from the neutral shape: tool calls ride
    on the assistant message, but each tool result is its own `role: "tool"` message.
    """
    role = message["role"]
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []

    for block in message["content"]:
        kind = block.get("type")
        if kind == "text":
            text_parts.append(block["text"])
        elif kind == "tool_use":
            tool_calls.append(
                {
                    "id": block["id"],
                    "type": "function",
                    "function": {
                        "name": block["name"],
                        "arguments": json.dumps(block["input"]),
                    },
                }
            )
        elif kind == "tool_result":
            tool_results.append(
                {
                    "role": "tool",
                    "tool_call_id": block["tool_use_id"],
                    "content": block["content"],
                }
            )

    out: list[dict[str, Any]] = []
    if role == "assistant" and tool_calls:
        entry: dict[str, Any] = {"role": "assistant", "tool_calls": tool_calls}
        if text_parts:
            entry["content"] = "\n".join(text_parts)
        out.append(entry)
    elif text_parts:
        out.append({"role": role, "content": "\n".join(text_parts)})

    out.extend(tool_results)
    return out


# The gateway has no tool-calling surface, so tool calls travel as a text protocol.
# Same shape as Reference/MCP-POC-AGENTS/.../common/utils.py, which is what these
# models are known to comply with.
#
# Only tool calls are wrapped. A final answer stays plain text, so the build prompt's
# trailing fenced JSON block needs no escaping inside a JSON string field.
_GATEWAY_PROTOCOL = """\
## HOW TO CALL TOOLS

You cannot call tools directly. To call one or more tools, reply with ONLY this JSON
object and nothing else — no prose before or after, no markdown fences:

{"action": "tool_call", "tool_calls": [{"id": "<short unique id>", "name": "<tool name>", "args": {<arguments>}}]}

Rules:
- Use exact tool names and argument keys from the schemas below. Invent nothing.
- Emit either tool calls OR a final answer in one turn, never both.
- Tool results come back in the next turn, each labelled with its id.
- When you are done calling tools, answer normally as plain text, following the
  output contract in your instructions above (including its fenced JSON block).

## TOOLS

"""


class GatewayLLM:
    """Intelligize gateway adapter — `POST /api/IntelligizeAI`.

    Models are addressed by (account_type, endpoint_secret_key), and the endpoint is
    text-in/text-out: one prompt string, one string back. No tool surface, no message
    array, no usage. Tool calling therefore lives in the prompt (_GATEWAY_PROTOCOL),
    with two consequences:

      - Token counts are 0 here. The gateway reports no usage, and an estimate would
        read as measured next to Bedrock's. Timings are real on both.
      - Protocol compliance is prompt-level. A reply that ignores it becomes a final
        answer rather than a malformed tool call.

    Worth it for the model list: real GPT-5.x and Gemini ids, unlike Bedrock's
    open-weight `gpt-oss`.
    """

    def __init__(
        self,
        endpoint_secret_key: str,
        account_type: int,
        url: str | None = None,
        timeout: int | None = None,
        max_response_tokens: int = 32000,
    ):
        from shared.config import GATEWAY_TIMEOUT, GATEWAY_URL

        self.endpoint_secret_key = endpoint_secret_key
        self.account_type = account_type
        self.url = url or GATEWAY_URL
        self.timeout = timeout or GATEWAY_TIMEOUT
        self.max_response_tokens = max_response_tokens
        self.name = f"gateway:{endpoint_secret_key}"

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Completion:
        import httpx

        prompt = self._render(system, messages, tools)
        body = {
            "prompt": prompt,
            "question": "",
            "sourceContent": "",
            "companyName": "",
            "formType": "",
            # The gateway treats this closer to a character budget than a token one.
            "responseMaxTokens": self.max_response_tokens,
            "intelligizeAIAccountType": self.account_type,
            "endpointSecretKey": self.endpoint_secret_key,
            "PromptId": 1,
            "ModelId": "",
        }

        try:
            response = httpx.post(self.url, json=body, timeout=self.timeout)
            response.raise_for_status()
        except httpx.ReadTimeout as exc:
            # Not "gateway down" — it accepted the request. The loop cannot recover,
            # so one slow turn loses the build; name the knob that fixes it.
            raise RuntimeError(
                f"The Intelligize AI gateway did not answer within {self.timeout}s "
                f"({self.endpoint_secret_key}). Long boolean keyword lists are the "
                "usual cause. Raise INTELLIGIZE_AI_TIMEOUT and retry."
            ) from exc
        except httpx.ConnectError as exc:
            # "Connection refused on 6043" does not suggest the missing tunnel.
            raise RuntimeError(
                f"Cannot reach the Intelligize AI gateway at {self.url}. Open the SSM "
                "port forward first:\n  aws ssm start-session --target "
                "<nbs-dev-web-ec2-id> --document-name AWS-StartPortForwardingSession "
                '--parameters "localPortNumber=6043,portNumber=6043" --region '
                f"us-east-1 --profile <dev profile>\n({type(exc).__name__}: {exc})"
            ) from exc

        text = _unwrap_gateway_text(response.text or "")
        parsed = _parse_gateway_reply(text)
        return Completion(
            text=parsed[0],
            tool_calls=parsed[1],
            stop_reason="tool_use" if parsed[1] else "end_turn",
            # No usage from the gateway; see the class docstring.
            input_tokens=0,
            output_tokens=0,
            raw={"prompt_chars": len(prompt), "response_chars": len(text)},
        )

    def _render(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> str:
        """Flatten system prompt, tool protocol and history into one prompt string."""
        parts = [system]
        if tools:
            parts.append(_GATEWAY_PROTOCOL + "\n\n".join(_render_tool(t) for t in tools))
        parts.extend(_render_gateway_message(m) for m in messages)
        # Without this cue the model continues the transcript instead of answering.
        parts.append("assistant:")
        return "\n\n".join(p for p in parts if p)


def _render_tool(tool: dict[str, Any]) -> str:
    schema = json.dumps(tool.get("inputSchema") or {}, indent=2)
    return (
        f"### {tool.get('name')}\n{tool.get('description') or ''}\n\n"
        f"Arguments (JSON Schema):\n```json\n{schema}\n```"
    )


def _render_gateway_message(message: dict[str, Any]) -> str:
    """Neutral loop history -> labelled plain text, ids kept so calls pair up."""
    role = message["role"]
    lines: list[str] = []
    for block in message["content"]:
        kind = block.get("type")
        if kind == "text":
            lines.append(block["text"])
        elif kind == "tool_use":
            lines.append(
                f'[tool call id={block["id"]}] {block["name"]}'
                f"({json.dumps(block['input'], default=repr)})"
            )
        elif kind == "tool_result":
            status = "ERROR" if block.get("is_error") else "ok"
            lines.append(
                f'[tool result id={block["tool_use_id"]} {status}]\n{block["content"]}'
            )
    return f"{role}:\n" + "\n\n".join(lines)


def _unwrap_gateway_text(raw: str) -> str:
    """The gateway sometimes returns a JSON-encoded string rather than raw text."""
    text = (raw or "").strip()
    try:
        decoded = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text
    if isinstance(decoded, str):
        return decoded.strip()
    # Some endpoint keys return a dict instead.
    if isinstance(decoded, dict):
        for key in ("response", "content", "text", "result", "answer"):
            value = decoded.get(key)
            if isinstance(value, str):
                return value.strip()
    return text


def _parse_gateway_reply(text: str) -> tuple[str, list[ToolCall]]:
    """Split a gateway reply into (text, tool_calls).

    Tolerant because models fence the object or prefix it with prose, and a strict
    parser would lose the turn. Anything unrecognisable is returned as text, which the
    loop treats as a final answer — as it does a real provider's end_turn.
    """
    stripped = text.strip()
    for candidate in _json_objects(stripped):
        if candidate.get("action") != "tool_call":
            continue
        calls: list[ToolCall] = []
        for raw_call in candidate.get("tool_calls") or []:
            if not isinstance(raw_call, dict):
                continue
            name = raw_call.get("name")
            if not name:
                continue
            args = raw_call.get("args")
            if not isinstance(args, dict):
                args = {}
            calls.append(
                ToolCall(
                    id=str(raw_call.get("id") or f"call_{len(calls) + 1}"),
                    name=str(name),
                    arguments=args,
                )
            )
        if calls:
            return ("", calls)

    # Final answer, fences included — parse_plan wants that block.
    return (stripped, [])


def _json_objects(text: str) -> list[dict[str, Any]]:
    """Every top-level JSON object in `text` — position is not assumed."""
    found: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            found.append(obj)
    return found
