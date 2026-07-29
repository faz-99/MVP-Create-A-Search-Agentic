# Deploying AI Create Search

Commands are for **Windows `cmd.exe`**. Run them from the project root
(`D:\NBS Projects\CreateSearchAgentic`).

> Docker was not installed on the machine this was written on, so the image has
> **not** been built or run yet. The build is unverified — expect to iterate on the
> first `docker compose build`. Everything below is the intended sequence, and the
> Troubleshooting section covers the failures most likely to hit first.

---

## 0. Prerequisites

```cmd
docker --version
docker compose version
```

Both must print a version. If `docker` is not recognised, install Docker Desktop
and make sure it is **running** (the whale icon in the tray) before continuing.

---

## 1. Set up `.env`

The container reads secrets from `.env` via `env_file`. It is gitignored and is
**not** baked into the image.

```cmd
copy .env.example .env
notepad .env
```

Required:

```
LEXIS_SSO_COOKIE=<the LexisObSSOCookie value>
```

These cookies expire. If the UI starts but every search 401s, this is the first
thing to refresh.

Optional, only for the OpenAI leg on the deploy target:

```
AWS_BEARER_TOKEN_BEDROCK=<bedrock api key>
OPENAI_BASE_URL=https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1
```

Leave those blank locally — the OpenAI leg falls back to Bedrock Converse.

---

## 2. Check AWS credentials are visible

Claude runs on Bedrock, so the container needs AWS credentials. `docker-compose.yml`
mounts `%USERPROFILE%\.aws` read-only; nothing is copied into the image.

```cmd
type %USERPROFILE%\.aws\credentials | findstr "["
aws sts get-caller-identity --profile intelligize-dev
```

The profile named in `docker-compose.yml` (`AWS_PROFILE`, default
`intelligize-dev`) must exist in that file. To use a different one:

```cmd
set AWS_PROFILE=some-other-profile
docker compose up -d
```

On EC2/ECS, delete the `.aws` volume mount and rely on the instance/task role
instead — boto3 picks it up with no other change.

---

## 3. Build

```cmd
docker compose build
```

First build takes a few minutes (base image + deps). Later builds reuse the
dependency layer unless `requirements.txt` changed.

```cmd
docker compose build --no-cache
```

Use `--no-cache` after changing dependencies if a stale layer is suspected.

---

## 4. Run

```cmd
docker compose up -d
docker compose ps
docker compose logs -f create-search
```

`ps` should show `create-search` as `Up` and, after ~15s, `(healthy)`.
`Ctrl+C` stops following logs; it does not stop the container.

Expected in the logs:

```
[entrypoint] starting uvicorn on 0.0.0.0:8080
```

A `WARNING: LEXIS_SSO_COOKIE is not set` line means step 1 was skipped — the UI
will load but every search will 401.

---

## 5. Test

**a. The UI is serving**

```cmd
curl -i http://127.0.0.1:8080/
```

Expect `HTTP/1.1 200 OK`. Then open <http://127.0.0.1:8080> in a browser.

**b. MCP auth and the tool gate work** — this is the real smoke test, since it
proves the cookie, the network path, and the gate all at once:

```cmd
curl http://127.0.0.1:8080/api/tools
```

Expect `"ok": true`, 63 tools, and 9 in `withheld_execute_tools`. If `ok` is
`false`, read the `error` string — it distinguishes an expired cookie (401) from
an upstream outage (503).

**c. Create a search end to end** (takes ~20–30s; it calls a model):

```cmd
curl -X POST http://127.0.0.1:8080/api/build -H "Content-Type: application/json" -d "{\"query\":\"8-K filings where the CEO resigned\",\"provider\":\"claude\"}"
```

Watch for `"type": "step"` events with timings, then a `"type": "plan"` event with
`"runnable": true`.

**d. The CLI, inside the container**

```cmd
docker compose run --rm create-search python -m probe.probe_mcp
docker compose run --rm create-search python -m agent_claude.agent "8-K filings where the CEO resigned"
```

`probe_mcp` is the fastest way to confirm credentials and see whether the gate has
drifted.

---

## 6. Everyday commands

```cmd
docker compose logs -f create-search        :: follow logs
docker compose restart create-search        :: restart, keep the image
docker compose down                         :: stop and remove the container
docker compose up -d --build                :: rebuild and restart after a code change
docker compose exec create-search bash      :: shell inside the running container
docker compose exec create-search ls -la /app/out   :: captured runs
```

Captured runs are on the host in `.\out` via the volume mount, so they survive
`down` and rebuilds.

---

## Troubleshooting

**`docker: command not found` / `error during connect`**
Docker Desktop is not installed or not running. Start it and wait for the whale
icon to go steady.

**Port 8080 already in use**
Something else holds the port — quite likely a local uvicorn from development.
Find and stop it:

```cmd
netstat -ano | findstr :8080
taskkill /PID <pid> /F
```

Or publish on a different host port by changing only the **left** side in
`docker-compose.yml`: `"9090:8080"`.

**`exec /app/docker-entrypoint.sh: no such file or directory`**
The script has CRLF line endings or a UTF-8 BOM, so the kernel looks for an
interpreter called `/bin/bash\r`. The Dockerfile strips both, so this means the
file was edited after the image was built — rebuild with `--no-cache`.

**Every search returns 401**
The `LexisObSSOCookie` has expired, or `LEXIS_SSO_COOKIE` is not reaching the
container. Check it arrived:

```cmd
docker compose exec create-search printenv LEXIS_SSO_COOKIE
```

Empty means `.env` is missing or was created after `up` — `docker compose up -d`
again to reload it.

**`/api/tools` returns 503**
Upstream, not you. The MCP host resolves to several pods behind an ELB and some
return 503; the client already retries 6 times. If every attempt fails, the pool
is genuinely unhealthy — wait and retry.

**`NoCredentialsError` / `ExpiredToken` from boto3**
The `.aws` mount or the profile name is wrong. Verify inside the container:

```cmd
docker compose exec create-search printenv AWS_PROFILE
docker compose exec create-search ls -la /home/searchagent/.aws
```

The directory must contain `credentials`. If it is empty, the host path did not
resolve — on Windows, confirm Docker Desktop has file sharing enabled for the
drive holding your user profile.

**`ValidationException: on-demand throughput isn't supported`**
A Bedrock model id is missing its `us.` inference-profile prefix. Anthropic ids
need it; `openai.gpt-oss-*` ids must **not** have it. See `shared/config.py`.

**Container is `Up` but never `(healthy)`**
The healthcheck probes `/` inside the container. Check the app actually started:

```cmd
docker compose logs create-search
docker compose exec create-search curl -i http://127.0.0.1:8080/
```

**Image is much larger than expected**
`Reference/` is a different application kept in this repo for reading and is
excluded by `.dockerignore`. Confirm it is not being copied:

```cmd
docker compose exec create-search ls /app
```

There should be no `Reference` directory.
