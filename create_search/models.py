"""Model clients and credentials for both loops.

OpenAI follows agentv4: model keys live in AWS Secrets Manager (MODELS_SECRETS), each
holding Model / ApiKey / RequestUri, and the Agents SDK talks to them through the
Responses API. Without MODELS_SECRETS (local dev) OPENAI_API_KEY / OPENAI_BASE_URL are
used instead. Secrets stay in memory; nothing is written to disk.

Claude goes through the Anthropic SDK: on Bedrock's Messages API (Mantle) with the normal
AWS credential chain (as agentv4), on the legacy InvokeModel path with
CLAUDE_BACKEND=bedrock-invoke, or directly with ANTHROPIC_API_KEY when
CLAUDE_BACKEND=anthropic.
"""

import json
import os
from typing import Any

import anthropic
from agents import OpenAIResponsesModel
from openai import AsyncOpenAI

from create_search import config

_secrets: dict[str, dict[str, Any]] | None = None
_openai_clients: dict[tuple[str, str | None], AsyncOpenAI] = {}
ClaudeClient = anthropic.AsyncAnthropic | anthropic.AsyncAnthropicBedrockMantle | anthropic.AsyncAnthropicBedrock
_claude_client: ClaudeClient | None = None


def _load_secrets() -> dict[str, dict[str, Any]]:
    """{"openai": {key: config}} from Secrets Manager, or {} when not configured."""
    global _secrets
    if _secrets is not None:
        return _secrets
    _secrets = {"openai": {}}
    name = os.getenv("MODELS_SECRETS")
    if not name:
        return _secrets
    import boto3

    client = boto3.session.Session().client("secretsmanager", region_name=config.AWS_REGION)
    raw = json.loads(client.get_secret_value(SecretId=name)["SecretString"])
    for key, value in raw.items():
        if "open_ai" in key:
            _secrets["openai"][key] = json.loads(value)[0]
    return _secrets


def openai_model_keys() -> list[str]:
    return sorted(_load_secrets()["openai"])


def openai_config(model: str | None) -> dict[str, str | None]:
    """{"model", "apikey", "baseurl"} for a secrets key or a plain model name."""
    entries = _load_secrets()["openai"]
    key = model or os.getenv("DEFAULT_OPENAI_MODEL_KEY", "")
    if key in entries:
        values = entries[key]
        return {
            "model": values["Model"],
            "apikey": values["ApiKey"],
            "baseurl": values["RequestUri"].split("v1", 1)[0] + "/v1",
        }
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "No OpenAI credentials: set MODELS_SECRETS (Secrets Manager, as agentv4) or "
            "OPENAI_API_KEY for local runs."
        )
    return {
        "model": model or config.DEFAULT_OPENAI_MODEL,
        "apikey": api_key,
        "baseurl": os.getenv("OPENAI_BASE_URL") or None,
    }


def get_openai_model(api_key: str, base_url: str | None, model_name: str) -> OpenAIResponsesModel:
    """One AsyncOpenAI client per (key, url), reused across requests."""
    key = (api_key, base_url)
    client = _openai_clients.get(key)
    if client is None:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        _openai_clients[key] = client
    return OpenAIResponsesModel(model=model_name, openai_client=client)


def get_claude_client() -> ClaudeClient:
    global _claude_client
    if _claude_client is None:
        if config.CLAUDE_BACKEND == "anthropic":
            _claude_client = anthropic.AsyncAnthropic()
        elif config.CLAUDE_BACKEND == "bedrock-invoke":
            _claude_client = anthropic.AsyncAnthropicBedrock(aws_region=config.AWS_REGION)
        else:
            _claude_client = anthropic.AsyncAnthropicBedrockMantle(aws_region=config.AWS_REGION)
    return _claude_client


def available_models() -> dict[str, Any]:
    keys = openai_model_keys()
    return {
        "openai": keys or [config.DEFAULT_OPENAI_MODEL],
        "claude": [config.DEFAULT_CLAUDE_MODEL],
        "DefaultOpenai": os.getenv("DEFAULT_OPENAI_MODEL_KEY") or (keys[0] if keys else config.DEFAULT_OPENAI_MODEL),
        "DefaultClaude": config.DEFAULT_CLAUDE_MODEL,
    }


async def close_clients() -> None:
    global _claude_client
    for client in list(_openai_clients.values()):
        await client.close()
    _openai_clients.clear()
    if _claude_client is not None:
        await _claude_client.close()
        _claude_client = None
