"""Model clients. OpenAI keys come from Secrets Manager (MODELS_SECRETS, as agentv4) or OPENAI_API_KEY locally."""

import json
import os
from typing import Any

import anthropic
from agents import OpenAIResponsesModel
from openai import AsyncOpenAI

from create_search import config

_secrets: dict[str, dict[str, Any]] | None = None
_openai_clients: dict[tuple[str, str | None], AsyncOpenAI] = {}
_claude_client = None


def _openai_secrets() -> dict[str, Any]:
    """{key: {Model, ApiKey, RequestUri}} for the open_ai_* entries; {} without MODELS_SECRETS."""
    global _secrets
    if _secrets is None:
        _secrets = {}
        if name := os.getenv("MODELS_SECRETS"):
            import boto3
            client = boto3.session.Session().client("secretsmanager", region_name=config.AWS_REGION)
            raw = json.loads(client.get_secret_value(SecretId=name)["SecretString"])
            _secrets = {k: json.loads(v)[0] for k, v in raw.items() if "open_ai" in k}
    return _secrets


def openai_config(model: str | None) -> dict[str, str | None]:
    """{"model", "apikey", "baseurl"} for a secrets key or a plain model name."""
    key = model or os.getenv("DEFAULT_OPENAI_MODEL_KEY", "")
    if values := _openai_secrets().get(key):
        return {"model": values["Model"], "apikey": values["ApiKey"], "baseurl": values["RequestUri"].split("v1", 1)[0] + "/v1"}
    if not (api_key := os.getenv("OPENAI_API_KEY")):
        raise RuntimeError("No OpenAI credentials: set MODELS_SECRETS (server) or OPENAI_API_KEY (local).")
    return {"model": model or config.DEFAULT_OPENAI_MODEL, "apikey": api_key, "baseurl": os.getenv("OPENAI_BASE_URL") or None}


def get_openai_model(api_key: str, base_url: str | None, model_name: str) -> OpenAIResponsesModel:
    """One AsyncOpenAI client per (key, url), reused across requests."""
    client = _openai_clients.get((api_key, base_url))
    if client is None:
        client = _openai_clients[(api_key, base_url)] = AsyncOpenAI(api_key=api_key, base_url=base_url)
    return OpenAIResponsesModel(model=model_name, openai_client=client)


def get_claude_client():
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
    keys = sorted(_openai_secrets())
    return {
        "openai": keys or [config.DEFAULT_OPENAI_MODEL],
        "claude": [config.DEFAULT_CLAUDE_MODEL],
        "DefaultOpenai": os.getenv("DEFAULT_OPENAI_MODEL_KEY") or (keys[0] if keys else config.DEFAULT_OPENAI_MODEL),
        "DefaultClaude": config.DEFAULT_CLAUDE_MODEL,
    }


async def close_clients() -> None:
    global _claude_client
    for client in _openai_clients.values():
        await client.close()
    _openai_clients.clear()
    if _claude_client is not None:
        await _claude_client.close()
        _claude_client = None
