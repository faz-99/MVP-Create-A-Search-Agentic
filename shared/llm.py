"""Model-call seam for the client-side tool loop.

The tool loop is provider-shaped, not gateway-shaped: it needs exactly one
capability — "given messages and tool schemas, return either tool calls or text".
Everything else (which MCP tools exist, how the payload is captured, how the gate
holds) is already provider-independent.

So the loop takes an `LLM` and does not care what is behind it:

  AnthropicLLM   direct Anthropic API, real vendor model ids
  OpenAILLM      direct OpenAI API, real vendor model ids
  GatewayLLM     Intelligize gateway, endpoint_secret_key + account_type

The gateway adapter is a stub because the gateway keys (aws_bedrock_claude_4_sonnet,
open_ai_gpt_5_2_2025_12_11) are not valid vendor model ids and cannot be passed to a
vendor SDK. Filling it in is a small, isolated change: implement `complete` and
nothing else moves.
"""

import json
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
            inferenceConfig={"maxTokens": 8192},
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


class GatewayLLM:
    """Intelligize gateway adapter — not implemented.

    The gateway addresses models by (account_type, endpoint_secret_key) rather than
    by vendor model id, so this cannot be written against a vendor SDK. To implement:
    construct the gateway client and map its tool-calling surface onto `Completion`.

    If the gateway exposes no tool-calling surface at all, that is worth knowing
    before any of this is wired up: the loop needs a model that can emit structured
    tool calls, and a text-only completion endpoint cannot drive it.
    """

    def __init__(self, endpoint_secret_key: str, account_type: int):
        self.endpoint_secret_key = endpoint_secret_key
        self.account_type = account_type
        self.name = f"gateway:{endpoint_secret_key}"

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Completion:
        raise NotImplementedError(
            "GatewayLLM is not implemented. It needs the Intelligize gateway client's "
            "tool-calling surface — specifically how to (a) pass tool schemas and "
            "(b) read structured tool calls back. Until then use AnthropicLLM or "
            "OpenAILLM with a real vendor model id."
        )
