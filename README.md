# AI Create Search

An agent that turns a plain-language request into a **runnable search** against the
DDC4 MCP server, plus the instructions for analysing whatever that search returns.
It does not run the search. A human does, by clicking Run.

Two model legs — Claude and OpenAI — run through **one shared prompt and one shared
tool loop**, so the only difference between them is the model. That is what makes
comparing them meaningful.

```
query ──▶ [1] create search ──▶ [2] run it ──▶ [3] analyse results
          model + resolvers      user-gated      external API
          no execution           no model        (not built here)
```

---

## Quick start

```sh
python -m venv .venv && .venv/Scripts/activate     # Windows
pip install -r requirements.txt
cp .env.example .env                                # then fill in LEXIS_SSO_COOKIE
```

```sh
python -m probe.probe_mcp                          # what does the server expose?
python -m uvicorn ui.server:app --port 8080        # then open http://127.0.0.1:8080
python -m agent_claude.agent "8-K filings where the CEO resigned"
python -m agent_claude.agent --run "..."           # also execute the search
```

Run everything from the project root so the `shared` package resolves.

The Bedrock legs need `bedrock:InvokeModel` on the AWS user in `AWS_PROFILE`. Without
it every build fails with `AccessDeniedException` before the model runs — that is an
IAM grant, not a code problem. To work without it, use a gateway leg: open the SSM
tunnel (below), set `ENABLE_GATEWAY_LEGS=1` in `.env`, and pick a `Gateway ·` leg in
the UI.

> Port 8000 is blocked by Windows on this machine (`winerror 10013`); 8080 works.
> `pkill` does not kill uvicorn on Windows — use
> `Get-NetTCPConnection -LocalPort 8080 | Stop-Process -Id {OwningProcess} -Force`.

---

## The three phases

**1 — Create the search** (automatic). The model reads the MCP tool schemas, resolves
entities and codes to ids, and emits a JSON block naming a target search tool, its
arguments, and analysis instructions. It **cannot** run a search: the nine search
executors are never registered as callable tools.

**2 — Run it** (user-gated, no model). A direct MCP `tools/call` with the payload. The
only action in the system that touches the search API.

**3 — Analyse** (user-gated, external). Not implemented here by design — an existing
internal API does this. `shared/analysis.py` is the seam; implement `run_analysis` and
nothing else changes.

---

## Layout

```
prompts/build_system.txt     the master prompt — one prompt, both legs
prompts/plan_fence.txt       the required output contract (substituted into the above)

shared/config.py             endpoints, model ids, credential loading
shared/prompts.py            loads prompts/*.txt
shared/mcp_client.py         MCP over streamable HTTP (auth, retries)
shared/llm.py                model adapters behind one `LLM` protocol
shared/loop.py               the client-side tool loop — phase 1
shared/toolsets.py           the gate: which tools each phase may touch
shared/plan.py               parse the emitted search plan
shared/validate.py           validate a plan against the real tool schema
shared/runner.py             phase 2 — execute a created search
shared/analysis.py           phase 3 — seam for the existing analysis API
shared/capture.py            write every payload + result to out/
shared/cli.py                terminal rendering

agent_claude/agent.py        Claude leg (CLI)
agent_openai/agent.py        OpenAI leg (CLI)
probe/probe_mcp.py           enumerate the tool surface, check the gate
ui/server.py                 FastAPI: /api/build, /api/run, /api/analyze, /api/tools
ui/static/index.html         single-page UI, no build step
docs/                        reference material
out/                         captured runs (gitignored)
```

---

## Things that are not obvious

These each cost real debugging time. They are recorded because the reasons are not
visible from the code alone.

### MCP auth is a custom header, not a bearer token

Auth is the `LexisObSSOCookie` value sent in an **`lna` header**. Sending it as
`Authorization: Bearer` returns `401 invalid_token`, and so does every other scheme
tried (raw `Authorization`, `X-Api-Key`, `Cookie:`, query param).

**This is why the tool loop runs client-side.** No provider's hosted MCP connector can
send an arbitrary header — Anthropic's takes `authorization_token`, OpenAI's takes
`authorization`, and both render only an `Authorization` header. Bedrock does not
support the MCP connector at all. The loop had to move into our process, and the
result is better anyway: identical loop for both legs.

### A 503 does not mean the server is down

The host resolves to **three pods behind a Kubernetes ELB** and some return 503 while
others serve fine. A single 503 says only that this connection landed on a bad
backend. `MCPClient` retries (6 attempts, linear backoff); without it requests fail at
random. Transport errors (DNS blips, resets) are retried the same way and wrapped as
`MCPError`, because callers only catch `MCPError` and a raw `httpx.ConnectError`
reaching a route produces an empty 500 with no explanation.

### There is no single `/Search` tool

The server exposes **one search executor per dataset** — nine of them (SEC filings,
agreements and exhibits, comment letters, ESG, transcripts, SEC rules, no-action
letters, filing sections, accounting standards). Choosing the dataset is the agent's
first real decision, because the dataset determines which filters exist. That is why a
created search must name its target tool: `{"tool": ..., "payload": ...}`.

### Filter values are ids, not words

`formTypes: ["10-K"]` is rejected — the field wants integers, and `10-K` is `Id: 2`,
obtainable only via `sec_filings_find_form_types`. The same holds for SIC/NAICS codes,
sections, topics, exchanges, indexes and countries. This is the single most common way
a created search fails, so the prompt says *resolve first, then build*.

### Withholding the executors also hid their schemas

The first working run invented three field names (`formTypeIds`, `query`, `limit`)
because the tool it was building arguments for was not in its tool list — so it could
not see the schema. The fix is `describe_search_tool`, a synthetic **read-only** tool
that returns an executor's schema without making it callable. Reading a schema is not
running a search.

### The gate can spring a leak when the server renames a tool

`dbm_sections_search` was renamed to `filing_sections_search` mid-project, and because
the executor list is hardcoded, the renamed tool silently became callable during the
build phase. A hardcoded allowlist cannot notice a rename, so
`unclassified_search_tools()` flags any search-looking tool that is in neither the
executor nor the lookup list. The probe and the loop both surface it.

Many tools contain "search" but only look things up (`company_search`,
`search_sic_codes`, `*_search_*_mappings`), so pattern matching alone cannot classify
them — hence two explicit lists plus a drift check.

### The tool surface is advertised regardless of entitlement

All 63 tools are listed whatever the credential is licensed for. An unentitled dataset
fails only at run time with a **403** — `esg_search_reports` does this on a cookie
carrying 2 entitlements where an earlier one carried 9. `shared/runner.py` names that
failure class explicitly, because rebuilding the search will never fix it.

### Bedrock model-id prefixes are inconsistent

- Anthropic **requires** the `us.` inference-profile prefix. The bare id is rejected:
  *"Invocation of model ID … with on-demand throughput isn't supported."*
- OpenAI `gpt-oss` **requires the bare id**. `us.openai.gpt-oss-120b-1:0` is rejected
  as an invalid identifier.

Also: the Anthropic SDK's Mantle client is a separate IAM surface and 403s
(`bedrock-mantle:Create*`) for this user. boto3 `bedrock-runtime` works.

---

## Model legs

| Leg | Local | Deployed |
|---|---|---|
| `claude` | Bedrock Converse, `us.anthropic.claude-sonnet-4-20250514-v1:0` | same |
| `openai` | falls back to Bedrock `openai.gpt-oss-120b-1:0` | OpenAI SDK → AWS OpenAI-compatible endpoint |
| `gateway-claude` | Intelligize gateway, `aws_bedrock_claude_4_sonnet` | not offered |
| `gateway-openai` | Intelligize gateway, `open_ai_gpt_5_2_2025_12_11` | not offered |

The gateway legs are **local only** and hidden unless `ENABLE_GATEWAY_LEGS=1`. They
exist because Bedrock access is an IAM grant a dev machine may lose — and because the
gateway is the only route to a real GPT-5.x id. The UI asks `/api/providers` for the
list rather than hardcoding it, so the deploy host shows exactly the two Bedrock legs
it can actually run.

The OpenAI leg is meant to run the **OpenAI SDK against an AWS-hosted
OpenAI-compatible endpoint**, which only works on the deploy target. That surface is
**Chat Completions**, not the Responses API, which is what `OpenAILLM` is written
against. Locally there is no credential for it, so `pick_llm` falls back to the same
family over Converse — the leg stays exercisable in dev and switches automatically once
`AWS_BEARER_TOKEN_BEDROCK` (or `OPENAI_BASE_URL`) is present.

> **`gpt-oss` is not GPT-5.x.** Bedrock does not serve `open_ai_gpt_5_2_2025_12_11`.
> Do not read a local gpt-oss result as a verdict on the OpenAI leg. For a real
> GPT-5.2 comparison use the `gateway-openai` leg, which addresses that id directly.

### The gateway is text-only, and drives the loop anyway

`GatewayLLM` was a stub with an open question attached: the loop needs structured tool
calls back, so a text-only completion endpoint could not drive it. The gateway **is**
text-only — `POST /api/IntelligizeAI` takes one `prompt` string and returns one string,
with no message array, no tool surface, and no usage data.

It still works, by carrying tool calls as a prompt-level JSON protocol: schemas are
rendered into the prompt, the model replies with
`{"action": "tool_call", "tool_calls": [...]}`, and the adapter parses that back into
`Completion.tool_calls`. This is the shape `Reference/` already uses against the same
gateway, reused rather than reinvented because it is what these models comply with.
Two things follow, both worth knowing before reading the numbers:

- **Token columns are 0 on gateway legs.** The gateway reports no usage. An estimate
  would sit next to Bedrock's measured counts and read as if it were measured too.
  Timings are real on both.
- **Protocol compliance is prompt-level.** On Bedrock a malformed tool call cannot
  happen; here it is merely unlikely, and a reply that ignores the protocol is treated
  as a final answer.

A final answer stays plain text rather than being wrapped in the protocol JSON — the
build prompt already requires a trailing fenced JSON block, and nesting a fenced block
inside a JSON string field is an escaping trap for no benefit.

### The gateway is only reachable through an SSM tunnel

From **2026-07-27** `https://ddc4-ai.intelligize.net` no longer resolves publicly; it
answers `403 Access AI API using SSM port forwarding.` Open a port forward first, then
the default `INTELLIGIZE_AI_URL` (`http://localhost:6043/api/IntelligizeAI`) works:

```sh
aws ssm start-session --target <nbs-dev-web-ec2-id> \
  --document-name AWS-StartPortForwardingSession \
  --parameters "localPortNumber=6043,portNumber=6043" \
  --region us-east-1 --profile <dev profile>
```

The current EC2 id is on the status page. `GatewayLLM` turns the resulting
`ConnectError` into a message naming this command, because "connection refused on
6043" does not suggest it.

---

## Correctness of the created search

Three layers, because "looks plausible" is not enough:

1. **Schema access** — `describe_search_tool` lets the model read the executor's
   schema before writing arguments.
2. **Validation with repair** — the plan is validated with `jsonschema` against the
   live schema; on failure the model gets the specific errors and up to
   `MAX_REPAIRS` (2) attempts to fix itself. The schemas set
   `additionalProperties: false`, so invented fields are caught mechanically.
3. **Run is blocked while invalid** — `plan.runnable` requires validation to pass, and
   `shared/runner.py` re-validates immediately before calling, because the payload is
   operator-editable in the UI.

---

## Observability

Every step is timed and every model call's tokens counted, shown in the UI as a table
plus tiles, and printed by the CLI. **Overhead** (wall-clock minus model minus tool) is
broken out separately so our own parsing and validation cost cannot hide inside the
total. Timing lives in the loop, not the adapters, so both legs are measured
identically.

Every payload sent and result received is written to `out/<timestamp>-<provider>.json`
in the same shape for both legs, so runs can be diffed across providers.

Capture failure is **non-fatal** and reported as a `warning` event. It writes from a
`finally`, so a raised exception there replaced the returned plan — on the deploy host
an unwritable `out/` (bind mount owned by another uid) turned a good run into an error
with no plan, and the UI's Run button never appeared. A lost log file must not cost the
deliverable; if history stops appearing, check for that warning.

---

## Known open items

- **Search strategy is loose.** A CEO-resignation search matched `CEO (resigned` inside
  an executive-compensation table — schema-valid, but not the intent. Scoping to
  `sectionIds` (e.g. `Item 5.02`) would tighten it. Prompt tuning, not a bug.
- **~48k input tokens per run**, mostly the 55 tool schemas resent every turn. The tool
  list is identical each turn and is the obvious prompt-cache target.
- **Analysis is not wired** — pending the existing API. See `shared/analysis.py`.
- **`resolve_country("Canada")` returns `{}`** despite promising exact case-insensitive
  country matching. Unexplained; may need `resolve_region` instead.
- **Response shapes differ per dataset.** `sec_filings_search` and `transcripts_search`
  return `total`; `comment_letters_search_comments_responses` does not. Nothing
  downstream should assume a universal `total`.

---

## Credentials

`.env` is gitignored and holds `LEXIS_SSO_COOKIE` (the `lna` header value; these
expire — refresh from a logged-in session when auth starts failing). AWS credentials
come from `~/.aws/credentials` via the `intelligize-dev` profile; nothing AWS is
written into the repo.
