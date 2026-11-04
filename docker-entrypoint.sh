#!/bin/bash
# =============================================================================
# Container entrypoint — runs as the `searchagent` user (UID 1000).
#
# Starts the Create Search API and its test page (api.server:app). Set
# APP_MODULE=ui.server:app to serve the old POC instead. Captured runs are written
# to /app/out, which docker-compose mounts from the host so history survives rebuilds.
#
# `exec "$@"` lets you override the command (e.g. run the CLI instead of the
# server) while still getting tini as PID 1:
#   docker compose run --rm create-search python -m create_search "8-Ks of Apple"
#
# The guard on "[" catches a common Docker mistake — an unquoted JSON-style CMD
# such as `CMD [uvicorn, ui.server:app]` makes /bin/sh try to run "[uvicorn," and
# exit 127. Rather than fail obscurely, fall through to the known-good command.
# =============================================================================
set -euo pipefail

mkdir -p /app/out

# The bind mount masks the image's ownership, so a host out/ created by root leaves
# this process unable to write. Capture failure is non-fatal now, so without this
# check nothing would point at the cause.
if ! touch /app/out/.write-probe 2>/dev/null; then
  echo "[entrypoint] WARNING: /app/out is not writable by UID $(id -u)." >&2
  echo "[entrypoint]   Captured run history will be skipped (searches still work)." >&2
  echo "[entrypoint]   Fix on the host:  sudo chown -R 1000:1000 ./out" >&2
else
  rm -f /app/out/.write-probe
fi

APP_HOST="${APP_HOST:-0.0.0.0}"
APP_PORT="${APP_PORT:-8080}"
APP_MODULE="${APP_MODULE:-api.server:app}"

# Fail fast with a readable message rather than a stack trace on the first
# request. The cookie expires, so a missing or stale value is the single most
# likely reason a fresh deployment does not work. A request's own `lna` cookie or
# header takes precedence; this is the fallback outside ENV=prod.
if [ -z "${LEXIS_SSO_COOKIE:-}" ]; then
  echo "[entrypoint] WARNING: LEXIS_SSO_COOKIE is not set." >&2
  echo "[entrypoint]   Requests without an lna cookie/header will get 403 until it is" >&2
  echo "[entrypoint]   provided via env_file (.env). The page will still load." >&2
fi

# The OpenAI loop reads its model keys from Secrets Manager (as agentv4).
if [ "$APP_MODULE" = "api.server:app" ] && [ -z "${MODELS_SECRETS:-}" ] && [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "[entrypoint] WARNING: neither MODELS_SECRETS nor OPENAI_API_KEY is set." >&2
  echo "[entrypoint]   The OpenAI provider will fail; Claude (Bedrock) is unaffected." >&2
fi

if [ "$#" -gt 0 ]; then
  case "$1" in
    \[*)
      echo "[entrypoint] ignoring malformed CMD (starts with '['); using default" >&2
      ;;
    *)
      exec "$@"
      ;;
  esac
fi

echo "[entrypoint] starting ${APP_MODULE} on ${APP_HOST}:${APP_PORT}"
exec python -m uvicorn "$APP_MODULE" --host "$APP_HOST" --port "$APP_PORT"
