# AI Create Search — MCP-driven agent MVPs

Two MVPs of the same search agent, one per provider, both pointed at the DDC4 MCP
server. The server exposes the search-building tools directly, so neither MVP
implements a planning layer, a query-builder prompt chain, or client-side tool
dispatch — the provider connects to the MCP server, discovers the tools, and runs
the loop. The agent is a system prompt plus a tool surface.

The reference document in `docs/` describes a conventional pipeline approach. It is
background on the domain and requirements only; the architecture here deliberately
differs.

## Layout

```
shared/config.py        server URL, model ids, shared system prompt, token loading
probe/probe_mcp.py      tools/list against the MCP server — run this first
mvp_claude/agent.py     MVP A — Claude Messages API, MCP connector
mvp_openai/agent.py     MVP B — OpenAI Responses API, hosted MCP tool
docs/                   reference material
```

## Setup

```sh
python -m venv .venv && .venv/Scripts/activate     # Windows
pip install -r requirements.txt
cp .env.example .env                                # then fill it in
```

`.env` holds the DDC4 bearer token and your provider key. It is gitignored — keep
the token out of source and out of chat.

## Run

All commands from the project root, so the `shared` import resolves.

```sh
python -m probe.probe_mcp                # what tools does the server expose?
python -m probe.probe_mcp --full         # ...with full input schemas

python -m mvp_claude.agent               # interactive
python -m mvp_openai.agent "find 10-K filings mentioning goodwill impairment"
```

## How the two differ

|                    | MVP A (Claude)                         | MVP B (OpenAI)                          |
| ------------------ | -------------------------------------- | --------------------------------------- |
| Surface            | Messages API, `client.beta.messages`   | Responses API, `client.responses`       |
| MCP wiring         | `mcp_servers` + `mcp_toolset` tool     | single `{"type": "mcp"}` tool           |
| Beta gate          | `mcp-client-2025-11-20`                | none                                    |
| MCP auth field     | `authorization_token` on the server    | `authorization` on the tool             |
| Conversation state | full `messages` history resent         | `previous_response_id` chaining         |
| Tool results       | `mcp_tool_use` / `mcp_tool_result`     | `mcp_call` output items                 |
| Approval gate      | server-side, always auto               | `require_approval` (set to `never`)     |

Both resend the MCP token on every request — OpenAI documents that it is not
stored server-side.

## Notes

- **Claude Opus 5 safety classifiers.** Opus 5 can decline a request outright
  (`stop_reason: "refusal"`, HTTP 200). `mvp_claude/agent.py` checks that before
  reading content and enables server-side fallbacks so a decline is re-served by
  the fallback model instead of coming back empty. If the
  `mcp-client` + `server-side-fallback` beta combination is ever rejected, set
  `USE_FALLBACKS = False`.
- **`pause_turn`.** A long server-side tool loop can pause; MVP A re-sends to
  resume, capped at `MAX_PAUSE_RESUMES`.
- **Prompt caching.** MVP A sets top-level `cache_control` so the MCP tool
  definitions and system prompt are not re-billed each turn of a conversation.
