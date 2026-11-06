"""Settings, read once from the environment."""

import os

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))  # usecwd: find the project .env from any entry point

# Laptops behind TLS inspection: trust the OS cert store (must run before any HTTP client exists).
if os.getenv("USE_SYSTEM_CERTS", "").strip().lower() in {"1", "true", "yes", "on"}:
    import truststore
    truststore.inject_into_ssl()


def env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


ENV = os.getenv("ENV", "dev")

# MCP
MCP_SERVER_URL = os.getenv("MCP_SERVER") or "https://ddc4-mcp-poc.intelligize.net/mcp"
MCP_SERVER_NAME = "Intelligize MCP"
AUTH_HEADER = os.getenv("AUTH_HEADER", "lna")
MCP_TIMEOUT = env_int("MCP_TIMEOUT", 60)
MCP_RETRY_ATTEMPTS = env_int("MCP_RETRY_ATTEMPTS", 5)  # unhealthy pods answer 503; a retry usually lands elsewhere


def dev_token() -> str:
    """Token for local runs (CLI, or the API without an lna cookie/header)."""
    return os.getenv("LEXIS_SSO_COOKIE") or os.getenv("LEXIS_TOKEN") or ""


# Limits
MAX_TURNS = env_int("MAX_TURNS", 20)
WARN_BEFORE = env_int("WARN_BEFORE", 3)  # turns left when the model is told to submit
MAX_TOOL_CALLS = env_int("MAX_TOOL_CALLS", 40)
MAX_SEARCH_RUNS = env_int("MAX_SEARCH_RUNS", 5)
SEARCH_PREVIEW_ROWS = env_int("SEARCH_PREVIEW_ROWS", 5)  # pageSize is clamped to this
MAX_LOOKUP_CHARS = env_int("MAX_LOOKUP_CHARS", 6000)
MAX_SUBMIT_REMINDERS = env_int("MAX_SUBMIT_REMINDERS", 2)

# Models
DEFAULT_OPENAI_MODEL = os.getenv("DEFAULT_OPENAI_MODEL", "gpt-5.4-2026-03-05")
DEFAULT_CLAUDE_MODEL = os.getenv("DEFAULT_CLAUDE_MODEL", "anthropic.claude-opus-5-5")  # plain ID with CLAUDE_BACKEND=anthropic
CLAUDE_BACKEND = os.getenv("CLAUDE_BACKEND", "bedrock")  # bedrock (Mantle) | bedrock-invoke | anthropic
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
CLAUDE_THINKING = os.getenv("CLAUDE_THINKING", "adaptive")  # adaptive | enabled (pre-4.6 models) | off
CLAUDE_THINKING_BUDGET = env_int("CLAUDE_THINKING_BUDGET", 4096)
CLAUDE_MAX_TOKENS = env_int("CLAUDE_MAX_TOKENS", 16000)

# Storage
STORAGE_PATH = os.getenv("STORAGE_PATH", "storage")
CAPTURE_DIR = os.getenv("CAPTURE_DIR", "out")
