"""Shared configuration for both agents.

Both agents talk to the same remote MCP server and share one master prompt
(shared/prompts.py), so the only difference between them is which provider drives
the tool loop.
"""

import os

from dotenv import load_dotenv

load_dotenv()

MCP_SERVER_URL = "https://ddc4-mcp-poc.intelligize.net/mcp"
MCP_SERVER_NAME = "ddc4"

# Models are addressed by Intelligize gateway endpoint_secret_key, not by vendor
# model id. account_type is part of the routing, so the two travel together.
INTELLIGIZE_ACCOUNT_BEDROCK = 2
INTELLIGIZE_ACCOUNT_GEMINI = 6

# Selected per the project decision: sonnet 4 on the Claude side, gpt 5.2 on the
# OpenAI side.
CLAUDE_ENDPOINT_KEY = "aws_bedrock_claude_4_sonnet"
CLAUDE_ACCOUNT_TYPE = INTELLIGIZE_ACCOUNT_BEDROCK

OPENAI_ENDPOINT_KEY = "open_ai_gpt_5_2_2025_12_11"
OPENAI_ACCOUNT_TYPE = INTELLIGIZE_ACCOUNT_BEDROCK

# Other keys available through the gateway, for A/B runs.
AVAILABLE_ENDPOINT_KEYS = {
    "bedrock": [
        "aws_bedrock_claude_4_5_sonnet",
        "aws_bedrock_claude_4_sonnet",
        "aws_bedrock_claude_3_5_sonnet",
        "aws_bedrock_claude_3_sonnet",
        "aws_bedrock_claude_3_haiku",
    ],
    "openai": [
        "open-ai-gpt-5.5-2026-04-23",
        "open_ai_gpt_5_4_2026_03_05",
        "open_ai_gpt_5_4_mini_2026_03_17",
        "open_ai_gpt_5_2_2025_12_11_priority",
        "open_ai_gpt_5_2_2025_12_11",
        "open_ai_gpt_5_1_2025_11_13",
        "open_ai_gpt_5_2025_08_07",
        "open_ai_gpt_5_mini_2025_08_07",
        "open_ai_gpt_4_1",
    ],
    "gemini": [
        "gemini_3_pro",
        "gemini_3_flash",
        "gemini_2_5_flash",
    ],
}

# Intelligize AI gateway. Since 2026-07-27 the public host answers `403 Access AI API
# using SSM port forwarding.`, so the default is the forwarded local port — see the
# tunnel command in README. Override with INTELLIGIZE_AI_URL from inside the VPN.
GATEWAY_URL = (
    os.environ.get("INTELLIGIZE_AI_URL") or "http://localhost:6043/api/IntelligizeAI"
)

# A normal turn takes 10–20s, but turns emitting a long boolean keyword list ran past
# 180s and killed the whole build with a bare ReadTimeout.
GATEWAY_TIMEOUT = int(os.environ.get("INTELLIGIZE_AI_TIMEOUT") or "300")

# Local only: the deploy host has no SSM tunnel, so these legs cannot work there.
# `.env` is excluded from the S3 sync, so a local value never reaches the server.
GATEWAY_LEGS_ENABLED = (os.environ.get("ENABLE_GATEWAY_LEGS") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Bedrock — the path for both legs. Tool calling verified with boto3 Converse.
#
# Two gotchas, both hit during setup:
#   - The Anthropic id needs the `us.` inference-profile prefix. The bare model id
#     (anthropic.claude-sonnet-4-20250514-v1:0) is rejected: "Invocation of model ID
#     ... with on-demand throughput isn't supported."
#   - The Anthropic SDK's Mantle client is a separate IAM surface and returns 403
#     (bedrock-mantle:Create*). boto3 bedrock-runtime works.
#
# Deliberately no default profile — the right answer differs by environment:
#
#   LOCAL  needs a named profile (AWS_PROFILE in .env). There is no instance role,
#          and the `default` profile is not necessarily the one with Bedrock access.
#   SERVER needs AWS_PROFILE unset, so boto3 falls through to the instance role.
#          Naming a profile that does not exist there raises ProfileNotFound —
#          which is exactly what a hardcoded "intelligize-dev" default caused.
#
# Empty string is treated as unset so a blank line in .env behaves like absence.
BEDROCK_PROFILE = os.environ.get("AWS_PROFILE") or None
BEDROCK_REGION = os.environ.get("AWS_REGION") or "us-east-1"

# Claude leg. Needs the `us.` inference-profile prefix.
BEDROCK_MODEL_ID = "us.anthropic.claude-sonnet-4-20250514-v1:0"

# OpenAI leg, also through Bedrock — same Converse API, same AWS credentials, so both
# legs run through one adapter and are measured identically.
#
# Note the prefix rule is INVERTED here: gpt-oss takes the bare model id and
# `us.openai.gpt-oss-120b-1:0` is rejected as an invalid identifier, whereas the
# Anthropic ids require the `us.` prefix. Both verified for tool calling.
#
# Caveat: these are the open-weight gpt-oss models, NOT GPT-5.x. If the comparison
# needs to be against open_ai_gpt_5_2_2025_12_11, that has to come from a real OpenAI
# key or the Intelligize gateway — Bedrock does not serve it.
OPENAI_BEDROCK_MODEL_ID = "openai.gpt-oss-120b-1:0"

# OpenAI SDK against an AWS-hosted OpenAI-compatible endpoint. This is the deploy
# target: it is expected NOT to work from a local machine (no IAM role / network
# path), so the code must fall back cleanly rather than appear broken in dev.
#
# The compatible surface is Chat Completions (/chat/completions), not the Responses
# API — OpenAILLM is written against chat.completions for exactly this reason.
#
# Auth is a bearer token, so the OpenAI SDK works unchanged: AWS_BEARER_TOKEN_BEDROCK
# is the conventional variable for a Bedrock API key. OPENAI_API_KEY also works if the
# endpoint accepts one.
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "")
OPENAI_COMPATIBLE_BEDROCK_URL = (
    f"https://bedrock-runtime.{BEDROCK_REGION}.amazonaws.com/openai/v1"
)


def openai_credentials() -> tuple[str, str] | None:
    """(base_url, api_key) for the OpenAI SDK, or None if unavailable here.

    Returning None rather than raising lets callers fall back to the Bedrock Converse
    path locally while the same code uses the OpenAI SDK once deployed.
    """
    key = os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or os.environ.get(
        "OPENAI_API_KEY"
    )
    if not key:
        return None
    return (OPENAI_BASE_URL or OPENAI_COMPATIBLE_BEDROCK_URL, key)

# Direct-vendor model ids, used only on the direct-API code path (needs a vendor
# API key rather than AWS credentials).
CLAUDE_MODEL = "claude-opus-5"
OPENAI_MODEL = "gpt-5.6"


class ConfigError(RuntimeError):
    """Missing or invalid configuration.

    A plain Exception, deliberately: SystemExit is a BaseException and would slip
    past the server's `except Exception` handling and kill the worker instead of
    returning an error to the browser.
    """


# The DDC4 MCP server authenticates with the LexisObSSOCookie value sent in a custom
# `lna` header — not `Authorization: Bearer`, and not a real Cookie header.
#
# This matters architecturally. Neither provider's hosted MCP connector can send an
# arbitrary header: Anthropic's takes `authorization_token`, OpenAI's takes
# `authorization`, and both render only an Authorization header. So server-side MCP
# connectors cannot authenticate here — the tool loop has to run client-side, through
# shared/mcp_client.py. See the TOOL LOOP note in shared/toolsets.py.
AUTH_HEADER_NAME = "lna"


def mcp_token() -> str:
    """The LexisObSSOCookie value used to authenticate to the MCP server."""
    token = os.environ.get("LEXIS_SSO_COOKIE") or os.environ.get("DDC4_MCP_TOKEN")
    if not token:
        raise ConfigError(
            "LEXIS_SSO_COOKIE is not set. Copy .env.example to .env and fill it in."
        )
    return token
