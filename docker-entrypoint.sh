#!/bin/bash
# =============================================================================
# Container entrypoint — runs as the `searchagent` user (UID 1000).
#
# Starts the FastAPI UI. Captured runs are written to /app/out, which
# docker-compose mounts from the host so history survives rebuilds.
#
# `exec "$@"` lets you override the command (e.g. run a CLI agent instead of the
# server) while still getting tini as PID 1:
#   docker compose run --rm create-search python -m agent_claude.agent "8-K ..."
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

# Fail fast with a readable message rather than a stack trace on the first
# request. The cookie expires, so a missing or stale value is the single most
# likely reason a fresh deployment does not work.
if [ -z "${LEXIS_SSO_COOKIE:-}" ]; then
  echo "[entrypoint] WARNING: LEXIS_SSO_COOKIE is not set." >&2
  echo "[entrypoint]   MCP calls will fail with 401 until it is provided via" >&2
  echo "[entrypoint]   env_file (.env) or the environment. The UI will still start." >&2
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

echo "[entrypoint] starting uvicorn on ${APP_HOST}:${APP_PORT}"
exec python -m uvicorn ui.server:app --host "$APP_HOST" --port "$APP_PORT"
