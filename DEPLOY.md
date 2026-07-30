# Deploying AI Create Search

Commands are for **Windows `cmd.exe`**. Run them from the project root
(`D:\NBS Projects\CreateSearchAgentic`).

> **Build verified** on the deploy host (`create-search-ai:latest`, ~70s, all 9
> layers). Not yet verified: a successful `up` — the first attempt hit a host port
> collision on 8080, which is why the published port is now **6073**.
>
> The container listens on **8080 internally** and is published on **6073**. Only the
> published port can collide; changing it never requires touching the Dockerfile or
> the healthcheck.

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

## 2. AWS credentials (Bedrock)

**Locally you need a profile; on the server you must not set one.** Both legs run
on Bedrock, and the credential source differs by environment:

| | `AWS_PROFILE` | Source |
|---|---|---|
| Local | **set** in `.env` (e.g. `intelligize-dev`) | `~/.aws/credentials` |
| Server | **unset** | instance role, via boto3's chain |

Check the local profile works:

```cmd
aws sts get-caller-identity --profile intelligize-dev
```

Running the container *locally* also needs the credentials file visible inside it —
uncomment the `.aws` mount in `docker-compose.yml`. It is commented out by default
because the server does not need it and nothing credential-shaped should enter the
image.

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
curl -i http://127.0.0.1:6073/
```

Expect `HTTP/1.1 200 OK`. Then open <http://127.0.0.1:6073> in a browser.

**b. MCP auth and the tool gate work** — this is the real smoke test, since it
proves the cookie, the network path, and the gate all at once:

```cmd
curl http://127.0.0.1:6073/api/tools
```

Expect `"ok": true`, 63 tools, and 9 in `withheld_execute_tools`. If `ok` is
`false`, read the `error` string — it distinguishes an expired cookie (401) from
an upstream outage (503).

**c. Create a search end to end** (takes ~20–30s; it calls a model):

```cmd
curl -X POST http://127.0.0.1:6073/api/build -H "Content-Type: application/json" -d "{\"query\":\"8-K filings where the CEO resigned\",\"provider\":\"claude\"}"
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

## Deploying to the server via S3

Separate S3 prefix and server directory from `protege_ai_agent`, so the two
deployments never overwrite each other.

| | Value |
|---|---|
| S3 prefix | `s3://nbs-prod-backup/create_search_agent/` |
| Server directory | `/data/create-search-agent` |
| Image / container | `create-search-ai:latest` / `create-search` |

### 1. Upload from the dev machine (cmd.exe)

`aws s3 cp` does **not** respect `.dockerignore`, so the excludes are mandatory
rather than tidiness. Two of them matter for real reasons: `.env` holds the
LexisObSSOCookie and must never land in a shared bucket, and `Reference/` is a
different application that would add hundreds of MB.

```cmd
cd /d "D:\NBS Projects\CreateSearchAgentic"

aws s3 cp --recursive ./ s3://nbs-prod-backup/create_search_agent/ ^
  --profile intelligize-dev ^
  --exclude ".env" ^
  --exclude ".git/*" ^
  --exclude ".venv/*" ^
  --exclude "Reference/*" ^
  --exclude "out/*" ^
  --exclude "docs/*" ^
  --exclude "**/__pycache__/*" ^
  --exclude "*.pyc" ^
  --exclude ".claude/*"
```

Verify what actually landed — cheaper than discovering a missing file on the
server:

```cmd
aws s3 ls --recursive s3://nbs-prod-backup/create_search_agent/ --profile intelligize-dev
```

Confirm `.env` is absent and `Dockerfile`, `docker-compose.yml`,
`docker-entrypoint.sh`, `requirements.txt`, `prompts/`, `shared/`, `ui/` are all
present.

### 2. Pull on the server

```bash
sudo mkdir -p /data/create-search-agent
cd /data/create-search-agent

aws s3 cp --recursive s3://nbs-prod-backup/create_search_agent/ ./
```

> **`cp --recursive` never deletes.** A file you renamed or removed locally stays
> on the server forever, and Python will happily import a stale module that no
> longer exists in the repo. After any rename or delete, use `sync` instead —
> `--exclude ".env"` protects the server's `.env` from `--delete`, because
> exclusions apply to the destination as well as the source:
>
> ```bash
> aws s3 sync s3://nbs-prod-backup/create_search_agent/ ./ --delete --exclude ".env"
> ```

### 3. One-time server setup

**`.env`** is deliberately not in S3, so create it once on the server. It survives
later syncs because it is excluded.

```bash
cd /data/create-search-agent
cp .env.example .env
vi .env          # set LEXIS_SSO_COOKIE
```

**AWS credentials for Bedrock — leave `AWS_PROFILE` unset on the server.**

This differs from local on purpose:

| | `AWS_PROFILE` | Credentials come from |
|---|---|---|
| Local | **set** (e.g. `intelligize-dev`) | `~/.aws/credentials`, plus the `.aws` mount if running in Docker |
| Server | **unset** | the instance role, via boto3's chain |

Setting it on the server is the failure that produced `ProfileNotFound:
The config profile (intelligize-dev) could not be found` — the profile does not
exist there and never will. `docker-compose.yml` therefore does not set it, and the
`.aws` mount is commented out so nothing credential-shaped enters the container.

Since `.env` is excluded from the S3 sync, the server's `.env` is independent of
yours — a local `AWS_PROFILE` will not leak into the deployment.

*Old note, for a host with no role:* ensure `~/.aws/credentials` exists for the user
running Docker and contains the profile named in `AWS_PROFILE`. The compose mount
  resolves `${HOME}` on Linux.

**Output directory** — created by the volume mount on first `up`, but it must be
writable by UID 1000 (`searchagent` in the image):

```bash
mkdir -p /data/create-search-agent/out
sudo chown -R 1000:1000 /data/create-search-agent/out
```

### 4. First deploy

```bash
cd /data/create-search-agent
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f create-search
```

Then smoke-test on the server itself:

```bash
curl -i http://127.0.0.1:6073/
curl http://127.0.0.1:6073/api/tools
```

`/api/tools` returning `"ok": true` with 63 tools and 9 withheld proves the
cookie, the network path to the MCP server, and the gate in one call.

### 5. Updating to a new version

```bash
cd /data/create-search-agent

docker compose down                                     # stop the container

# pull the new code (see the sync note above if files were renamed/deleted)
aws s3 cp --recursive s3://nbs-prod-backup/create_search_agent/ ./

chmod +x docker-entrypoint.sh                           # S3 drops the +x bit
docker compose build --no-cache                         # rebuild code + deps
docker compose up -d                                    # start back up
docker compose logs -f create-search
```

`--no-cache` is only strictly needed when `requirements.txt` changed; a plain
`build` is faster otherwise and still picks up code changes.

The `chmod` is belt-and-braces: S3 has no POSIX metadata so the executable bit is
lost on every download, and the Dockerfile already re-applies it during build.
Keeping it here means the file is also correct if anyone runs the script outside
Docker.

### Rollback

The image from the previous build is still on the server, so the fastest rollback
is to stop, restore the previous source, and rebuild. Tag before replacing if you
want a real rollback target:

```bash
docker tag create-search-ai:latest create-search-ai:prev   # before rebuilding
# ...to roll back:
docker compose down
docker tag create-search-ai:prev create-search-ai:latest
docker compose up -d --no-build
```

---

## Troubleshooting

**`docker: command not found` / `error during connect`**
Docker Desktop is not installed or not running. Start it and wait for the whale
icon to go steady.

**Published port already in use**
Something else holds the port — quite likely a local uvicorn from development.
Find and stop it:

```cmd
netstat -ano | findstr :6073
taskkill /PID <pid> /F
```

Or publish on a different host port by changing only the **left** side in
`docker-compose.yml`: `"6074:8080"`.

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
The healthcheck probes `/` on 8080 *inside* the container, which is unaffected by the host port. Check the app actually started:

```cmd
docker compose logs create-search
docker compose exec create-search curl -i http://127.0.0.1:6073/
```

**Image is much larger than expected**
`Reference/` is a different application kept in this repo for reading and is
excluded by `.dockerignore`. Confirm it is not being copied:

```cmd
docker compose exec create-search ls /app
```

There should be no `Reference` directory.
