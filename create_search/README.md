# create_search — agentic Create Search

Turns a research goal ("8-Ks of Apple filed this year") into **one executable search
payload**, plus a Search Strategy and analysis instructions. The agent may run searches
while it builds, to check its filters or find values it needs, but it only sees a short
preview, and its deliverable is the payload, not the results.

```sh
python -m create_search "8-Ks of Apple filed this year"                 # OpenAI loop
python -m create_search --provider claude --apps SF,AOE "..."           # Claude loop
python -m uvicorn api.server:app --port 8080                            # HTTP API
```

Solution overview and diagram: [SOLUTION.md](../SOLUTION.md). Deployment: [DEPLOY.md](../DEPLOY.md).

## Layout

```
create_search/
  agent.py             CreateSearchAgent(ProtegeBaseAgent): Intive template base; stream_research runs our loops
  run.py               entry point: create_search(query, provider, apps, token, ...)
  config.py            env settings and every limit
  clients.py           MCP connection (pooled HTTP/2, lna header) and model clients (Secrets Manager keys, Claude)
  toolset.py           bundles by tool-name prefix, search/content/lookup classes, drift
  executor.py          runs every tool call: budget, clamping, trimming, submit
  result.py            the agent's result (CreatedSearch) and the submit_search / report_strategy tools
  utils.py             events, stop files, system prompt, limit messages, validation, trimming, run recorder
  prompts/system.txt   the system prompt text
  loops/
    openai_loop.py     OpenAI Agents SDK, Responses API (long-term runtime)
    claude_loop.py     own loop on the Anthropic SDK (Bedrock), removable later
api/server.py          FastAPI: /api/create-search (SSE), /stop, /run, /models, /tools
ui/index.html          test page (served by the API at /)
agent_template/        Intive's agent template (docs in docs/agent-template/)
```

## How a request runs

1. Input check (empty or wordless requests are rejected before any model call).
2. MCP `tools/list`, then `select_tools`: shared `company_*` / `common_*` lookups plus
   the bundles for the requested apps (default SF, AOE; `ALL` for every bundle).
   Search tools are found from the live list by name (ending in `_search`). Document-content
   tools are never exposed. `drift()` logs tools or apps that no longer match a prefix.
3. The loop runs the model. Every tool call goes through `ToolExecutor`:
   - lookups: arguments validated against the schema, output cut at `MAX_LOOKUP_CHARS`;
   - searches: at most `MAX_SEARCH_RUNS`, `pageSize` clamped to `SEARCH_PREVIEW_ROWS`,
     result reduced to total + preview rows; a 403 is reported as "not entitled";
   - identical repeated calls are answered from the earlier result;
   - `report_strategy` streams the early Search Strategy to the UI;
   - `submit_search` validates the payload and either accepts it (the run ends) or
     returns the errors for the model to fix.
4. Limits: `MAX_TURNS` with a wrap-up notice `WARN_BEFORE` turns early, `MAX_TOOL_CALLS`,
   and `MAX_SUBMIT_REMINDERS` when a reply ends without a submit. If the run ends
   without a submit, the last search the agent ran successfully is returned with
   `source: "fallback"` and an unresolved condition saying so.
5. Events end with `usage`, `metrics` and `done`, whose message is:

```json
{
  "strategy": "...", "tool": "sec_filings_search", "payload": {...},
  "analysis_instructions": "...", "assumptions": [], "unresolved_conditions": [],
  "notes": "", "validation_errors": [], "source": "agent", "runnable": true
}
```

## Configuration

| Variable | Default | |
|---|---|---|
| `LEXIS_SSO_COOKIE` | | dev token for the `lna` header (API uses the cookie/header) |
| `MCP_SERVER` | ddc4-mcp-poc | MCP URL |
| `MODELS_SECRETS` | | Secrets Manager name with `open_ai_*` model keys (agentv4) |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | | local fallback when `MODELS_SECRETS` is unset |
| `DEFAULT_OPENAI_MODEL` / `DEFAULT_OPENAI_MODEL_KEY` | gpt-5.4-2026-03-05 | |
| `CLAUDE_BACKEND` | bedrock | Bedrock Messages API; `bedrock-invoke` (legacy InvokeModel) or `anthropic` (`ANTHROPIC_API_KEY`) |
| `DEFAULT_CLAUDE_MODEL` | anthropic.claude-opus-5-5 | Bedrock IDs keep the `anthropic.` prefix |
| `CLAUDE_THINKING` | adaptive | `adaptive` (current models), `enabled` (budgeted, pre-4.6 only), `off` |
| `USE_SYSTEM_CERTS` | off | `1` to trust the OS cert store (laptops behind TLS inspection) |
| `MAX_TURNS`, `WARN_BEFORE`, `MAX_TOOL_CALLS` | 20, 3, 40 | |
| `MAX_SEARCH_RUNS`, `SEARCH_PREVIEW_ROWS` | 5, 5 | |
