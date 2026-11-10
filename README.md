# Create Search Agent

Backend service that turns a plain-language request ("10-K filings of Microsoft from 2025") into **one executable
Intelligize search**, plus a search strategy and analysis instructions. The agent may run a few searches while it
builds, but it sees only a 5-row preview; the full search runs only when the user clicks **Run**.

Built on agentv4's patterns (MCP client, `lna` auth, Secrets Manager keys, event stream) and on Intive's agent
template (`agent_template/`). Architecture and design: [SOLUTION.md](SOLUTION.md). Deployment: [DEPLOY.md](DEPLOY.md).

## How it works

1. `POST /api/create-search` receives the request and streams events back (SSE).
2. The service connects to the Intelligize MCP server and exposes only the tools for the requested apps
   (default SF + AOE); document-content tools are never exposed.
3. A model loop (OpenAI or Claude) calls tools. Every call goes through `ToolExecutor`, which validates arguments,
   limits searches to 5 runs with a 5-row preview, and dedupes repeated calls.
4. The model delivers the result by calling `submit_search`. The payload is validated against the search tool's
   live schema; errors go back to the model to fix.
5. The stream ends with `usage`, `metrics` and `done` (the created search). `POST /api/run` executes it.

## Project layout

```
api/server.py               FastAPI app: endpoints, lna auth, SSE streaming
ui/index.html               test page (served by the API at /)
create_search/
  agent.py                  CreateSearchAgent (inherits agent_template's ProtegeBaseAgent)
  run.py                    pipeline: input check, tool selection, loop, final events
  config.py                 settings and limits from the environment
  clients.py                MCP connection and model clients (OpenAI, Claude)
  toolset.py                tool bundles per app, search/lookup/content classes, drift check
  executor.py               runs every tool call: validation, budgets, preview, submit
  result.py                 the agent's result (CreatedSearch) and the submit_search / report_strategy tools
  utils.py                  events, stop files, system prompt, validation, trimming, run recorder
  prompts/system.txt        system prompt
  loops/openai_loop.py      OpenAI Agents SDK loop (Responses API)
  loops/claude_loop.py      Claude loop on the Anthropic SDK
agent_template/             Intive's agent template (base class and helpers)
Dockerfile, docker-compose.yml, docker-entrypoint.sh
```

## Getting started

Requires Python 3.13 and an `lna` token (the `LexisObSSOCookie` value from a logged-in session).

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # then fill in the values below
```

Minimum `.env` for local development:

```ini
LEXIS_SSO_COOKIE=<lna token>
# Claude (local): use the Claude API directly
CLAUDE_BACKEND=anthropic
DEFAULT_CLAUDE_MODEL=claude-opus-5-5
ANTHROPIC_API_KEY=<key>
# OpenAI (local only; on the server keys come from MODELS_SECRETS)
OPENAI_API_KEY=<key>
# Corporate laptops behind TLS inspection
USE_SYSTEM_CERTS=1
```

`.env` holds secrets and is gitignored. Never commit it.

## Running

```powershell
python -m uvicorn api.server:app --port 8080
```

Open http://localhost:8080, pick a provider, enter a request and click **Create**. API docs: http://localhost:8080/docs.

Command line, without the server:

```powershell
python -m create_search --provider claude --apps SF,AOE "8-K filings where the CEO has resigned"
```

### Debugging (VS Code)

`.vscode/launch.json` has two configurations: **API (debug)** runs the server under the debugger (set breakpoints,
press F5, then use the page), and **CLI (debug)** runs one request. Useful breakpoints: `executor.py` → `execute`
(each tool call), `executor.py` → `_submit` (final search), `loops/claude_loop.py` after `messages.create`.

Every run is also saved in full to `out/<timestamp>-cs-<provider>.json`: each tool call with its untrimmed result,
timings, tokens and the final result.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/create-search` | `{Query, Id?, Provider, Model?, Apps?, Effort}`; streams `{"kind", "message"}` events |
| PUT | `/api/create-search/stop?id=` | Stop the run started with that `Id` |
| POST | `/api/run` | `{Tool, Payload}`; validates, then runs the search (422 with `validation_errors` if invalid) |
| GET | `/api/models` | Available models per provider |
| GET | `/api/tools?apps=SF,AOE` | Tools a request with those apps would see |
| GET | `/api/health` | Liveness |

Auth: the `lna` cookie, then the `lna` header; outside `ENV=prod`, `LEXIS_SSO_COOKIE` as a fallback.

Event kinds: `status`, `strategy`, `reasoning`, `trace`, `step`, `warning`, `usage`, `metrics`, `done`, `agenterror`,
`stop`, `ping`. The `done` message:

```json
{"strategy": "...", "tool": "sec_filings_search", "payload": {...}, "analysis_instructions": "...",
 "assumptions": [], "unresolved_conditions": [], "notes": "", "validation_errors": [],
 "source": "agent", "runnable": true}
```

`source` is `agent` (submitted), `fallback` (a limit ended the run; last valid search returned) or `null`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LEXIS_SSO_COOKIE` | | Dev `lna` token |
| `MCP_SERVER` | ddc4-mcp-poc | MCP URL |
| `MODELS_SECRETS` | | Secrets Manager secret with the `open_ai_*` model keys (server) |
| `DEFAULT_OPENAI_MODEL_KEY` | | Which `open_ai_*` entry to use by default |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | | Local fallback when `MODELS_SECRETS` is unset |
| `CLAUDE_BACKEND` | `bedrock` | `bedrock` (Bedrock Messages API), `bedrock-invoke`, or `anthropic` |
| `DEFAULT_CLAUDE_MODEL` | `anthropic.claude-opus-5-5` | Bedrock IDs keep the `anthropic.` prefix |
| `CLAUDE_THINKING` | `adaptive` | `adaptive`, `enabled` (older models), `off` |
| `AWS_REGION`, `AWS_PROFILE` | us-east-1 | AWS credentials chain; leave `AWS_PROFILE` unset on the server |
| `USE_SYSTEM_CERTS` | off | `1` to trust the OS certificate store |
| `ENV` | `dev` | `prod` disables `/docs` and the token fallback |
| `MAX_TURNS`, `WARN_BEFORE`, `MAX_TOOL_CALLS` | 20, 3, 40 | Loop limits |
| `MAX_SEARCH_RUNS`, `SEARCH_PREVIEW_ROWS`, `MAX_LOOKUP_CHARS` | 5, 5, 6000 | Executor limits |
| `CAPTURE_DIR`, `STORAGE_PATH` | `out`, `storage` | Run captures, stop files |

## Deployment

Docker, published on host port **6073**:

```bash
docker compose up -d --build
```

On the server, leave `AWS_PROFILE` unset (the instance role is used) and set `MODELS_SECRETS`; the role needs
`secretsmanager:GetSecretValue` on it, plus Bedrock access for Claude. Full steps and troubleshooting: [DEPLOY.md](DEPLOY.md).

## Troubleshooting

| Symptom | Cause |
|---|---|
| `403` from the API | No `lna` cookie/header, and no `LEXIS_SSO_COOKIE` (or `ENV=prod`) |
| `Failed to connect to MCP server ... 502/503` | MCP server outage or an unhealthy pod; retry later |
| `CERTIFICATE_VERIFY_FAILED` | TLS inspection on the laptop; set `USE_SYSTEM_CERTS=1` |
| `No OpenAI credentials` | Set `MODELS_SECRETS` (server) or `OPENAI_API_KEY` (local) |
| Claude `403` on Bedrock | IAM: grant `bedrock-mantle:CreateInference`, or use `CLAUDE_BACKEND=anthropic` locally |
