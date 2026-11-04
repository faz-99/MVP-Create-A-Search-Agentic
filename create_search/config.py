"""Settings for the Create Search agent, read once from the environment.

Every limit here is application-enforced (see core/limits.py): the prompt asks the model
to be economical, these make sure it is.
"""

import os

from dotenv import find_dotenv, load_dotenv

# usecwd: without it the search starts at the launching script's folder, which misses
# the project .env whenever the entry point lives elsewhere.
load_dotenv(find_dotenv(usecwd=True))

# Behind a TLS-inspecting proxy (corporate laptops) the proxy's root is in the OS store
# but not in certifi, so Bedrock calls fail with CERTIFICATE_VERIFY_FAILED. Opt-in, and
# done here because every module imports config before creating an HTTP client.
if os.getenv("USE_SYSTEM_CERTS", "").strip().lower() in {"1", "true", "yes", "on"}:
    import truststore

    truststore.inject_into_ssl()


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


ENV = os.getenv("ENV", "dev")

# --- MCP ---------------------------------------------------------------------
MCP_SERVER_URL = os.getenv("MCP_SERVER") or "https://ddc4-mcp-poc.intelligize.net/mcp"
MCP_SERVER_NAME = "Intelligize MCP"
# The server authenticates with the LexisObSSOCookie value in a custom header.
AUTH_HEADER = os.getenv("AUTH_HEADER", "lna")
MCP_TIMEOUT = env_int("MCP_TIMEOUT", 60)
# Unhealthy pods behind the ELB answer 503; a retry usually lands on a healthy one.
MCP_RETRY_ATTEMPTS = env_int("MCP_RETRY_ATTEMPTS", 5)


def dev_token() -> str:
    """Token for local runs (CLI, or the API without an lna cookie/header)."""
    return os.getenv("LEXIS_SSO_COOKIE") or os.getenv("LEXIS_TOKEN") or ""


# --- Limits ------------------------------------------------------------------
MAX_TURNS = env_int("MAX_TURNS", 20)
# Turns left at which the model is told to finalize instead of exploring further.
WARN_BEFORE = env_int("WARN_BEFORE", 3)
MAX_TOOL_CALLS = env_int("MAX_TOOL_CALLS", 40)
# Searches the agent may run while building. Results are only for its own judgement.
MAX_SEARCH_RUNS = env_int("MAX_SEARCH_RUNS", 5)
# Rows of a search result the model sees; pageSize is clamped to this during the build.
SEARCH_PREVIEW_ROWS = env_int("SEARCH_PREVIEW_ROWS", 5)
# Lookup results longer than this are cut (company_search can return a lot).
MAX_LOOKUP_CHARS = env_int("MAX_LOOKUP_CHARS", 6000)
# Replies that end without calling submit_search get this many reminders.
MAX_SUBMIT_REMINDERS = env_int("MAX_SUBMIT_REMINDERS", 2)

# --- Models ------------------------------------------------------------------
DEFAULT_OPENAI_MODEL = os.getenv("DEFAULT_OPENAI_MODEL", "gpt-5.4-2026-03-05")
# Bedrock IDs carry an "anthropic." prefix (claude-sonnet-4-20250514, the old default,
# has reached end of life there). Use the plain ID with CLAUDE_BACKEND=anthropic.
DEFAULT_CLAUDE_MODEL = os.getenv("DEFAULT_CLAUDE_MODEL", "anthropic.claude-opus-5-5")
# "bedrock"        Bedrock Messages API (Mantle) with AWS credentials, as agentv4;
# "bedrock-invoke" the legacy InvokeModel path, for "us.anthropic.*-v1:0" model IDs;
# "anthropic"      the Claude API directly (ANTHROPIC_API_KEY).
CLAUDE_BACKEND = os.getenv("CLAUDE_BACKEND", "bedrock")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
# "adaptive" (current models; effort comes from the request), "enabled" (budgeted,
# pre-4.6 models only; current models reject it) or "off" (older models only).
CLAUDE_THINKING = os.getenv("CLAUDE_THINKING", "adaptive")
CLAUDE_THINKING_BUDGET = env_int("CLAUDE_THINKING_BUDGET", 4096)
CLAUDE_MAX_TOKENS = env_int("CLAUDE_MAX_TOKENS", 16000)

# --- Storage -----------------------------------------------------------------
STORAGE_PATH = os.getenv("STORAGE_PATH", "storage")
CAPTURE_DIR = os.getenv("CAPTURE_DIR", "out")
