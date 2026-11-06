# AI Agent Service

This repository contains an async FastAPI service for chat and research workflows. Its current chat path uses an OpenAI agent built on `ProtegeAgentCore`, connects it to configured MCP tools, streams response events to clients, and persists conversation and session state locally.

## Requirements

- Python 3.11
- Access to the configured model secrets and any downstream MCP/auth services used by the request
- AWS credentials with permission to read the model secret when using AWS Secrets Manager

The service loads `.env` values with `python-dotenv`. On startup it loads provider model records from a local `secrets.json` cache, if present, or retrieves them from AWS Secrets Manager using `MODELS_SECRETS`.

## Run locally

From the repository root in PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt "fastapi>=0.115.0" "uvicorn[standard]>=0.30.0"
python api.py
```

The application listens on `http://localhost:8001` when launched with `python api.py`. Check `GET /api/health` for a basic health response. Interactive API documentation is enabled outside production; when `ENV=prod`, the application hides the FastAPI docs and OpenAPI endpoints.

Before launch, configure the model secret source, an OpenAI model key available in that configuration, and `MCP_SERVER`. Provide any required AWS identity and downstream authentication through your environment or secret-management setup. Do not put credentials in source files.

## OpenAI flow environment variables

The OpenAI chat path selects a model record from the loaded model configuration. That record supplies the model name, `ApiKey`, and `RequestUri`; the service derives the OpenAI-compatible base URL from `RequestUri`. The active chat path does not read `OPENAI_API_KEY` or `OPENAI_BASE_URL` directly.

| Variable | Default | Purpose and usage |
| --- | --- | --- |
| `MODELS_SECRETS` | None | AWS Secrets Manager secret name used by startup when `secrets.json` is not present. Configure this for the normal AWS-backed startup path. The code currently requests the secret in `us-east-1`. |
| `SECRET_NAME_OPEN_AI` | None | Fallback secret name used by `get_model_config` when the provider model map is unavailable. Normal startup initializes the model map through `MODELS_SECRETS` or the local cache. |
| `DEFAULT_OPENAI_MODEL` | `open_ai_gpt_5_6_terra` in chat service | Model key selected when an OpenAI chat request omits `Model`. It must match a key in the loaded OpenAI model configuration. The `/api/models` response has a separate fallback of `open_ai_gpt_5_4_2026_03_05`. |
| `OPENAI_MODEL` | None | Read by the shared `resolve_model_name()` helper, but the main `ApiService` chat path selects the configured model key directly; this variable does not override that selection. |
| `MCP_SERVER` | None | MCP server URL used by the OpenAI agent flow. Configure a reachable URL for chat requests; MCP tools are attached when the request source mode includes `Igz`. |
| `AUTH_HEADER` | `lna` | Header name used to forward the request's Lexis token to MCP. |
| `DEFAULT_EFFORT` | `Medium` | Reasoning effort assigned when the chat request does not specify one. |
| `MCP_TIMEOUT` | `60` seconds | MCP client-session timeout, shared by provider agent flows. |
| `MAX_TURNS` | `50` | Maximum agent turns for the run and context-size handling. |
| `WARN_BEFORE` | `5` | Number of turns remaining at which the context-size hook warns before the turn limit. |
| `TOKEN_KEY` | `lna` | Additional cookie/header name checked while resolving the Lexis token. The app also checks the `lna` cookie/header. |
| `LEXIS_TOKEN` | Empty | Optional server-side fallback Lexis token when a request does not provide one. Prefer the application's normal per-request authentication flow. |
| `ENV` | `dev` | Runtime environment label. Set to `prod` to disable FastAPI docs/OpenAPI and enable production cookie-auth behavior. |
| `KEEP_FILE` | `false` | Secret-cache handling. Set to `1` or `true` to retain `secrets.json`; otherwise the loader removes that file after reading or creating it. |
| `MCP_AUTH_URL` | None | URL used by separate MCP/auth utility flows. It is not the MCP agent endpoint; configure `MCP_SERVER` for the OpenAI agent connection. |
| `SANDBOX_TOOL_ENABLED` | `false` | Enables optional sandbox-agent tooling on supported non-Windows platforms. It does not enable that tool on Windows. |

`OPENAI_SERVICE_TIER` appears in the agent customization guide but is not read by the current service implementation. `OPENAI_API_KEY` and `OPENAI_BASE_URL` are likewise not the current chat configuration mechanism; credentials and endpoint come from the selected model record.

## Run the API container

The API container uses Python 3.11, installs `requirements.txt` plus FastAPI and Uvicorn, and listens on port 8000:

```powershell
docker build -f Dockerfile.api -t ai-agent-api .
docker run --rm -p 8000:8000 --env-file .env ai-agent-api
```

Ensure the container has access to the required AWS identity when `MODELS_SECRETS` is used, along with network access to `MCP_SERVER` and other downstream services. The `.env` file should contain configuration only and must not be committed.

## Secrets and sensitive data

Do not commit API credentials, tokens, AWS credentials, or populated secret files. The model configuration contains provider credentials and is loaded through the secret/cache flow described above. Keep `.env` and `secrets.json` out of source control, and restrict access to persisted conversation, session, and tool-output files. Agent tracing is configured to include sensitive data, so trace output and captured tool inputs must also be treated as sensitive.
