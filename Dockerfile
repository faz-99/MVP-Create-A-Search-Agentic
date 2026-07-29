# =============================================================================
# AI Create Search — Dockerfile
# =============================================================================
# Builds a self-contained image running the FastAPI UI + the client-side MCP
# tool loop. Separate image from any other project in this repo.
#
# Build:  docker compose build
# Run:    docker compose up -d
# Logs:   docker compose logs -f create-search
# Shell:  docker compose exec create-search bash
# =============================================================================

FROM python:3.13-slim-bookworm

# OS-level deps:
#   - tini            — proper PID 1, so `docker stop` forwards SIGTERM and
#                       uvicorn shuts down instead of being killed at timeout
#   - ca-certificates — outbound HTTPS to *.intelligize.net and AWS Bedrock
#   - build-essential — only needed if a wheel is unpublished for this Python
#                       version; dropped in the same layer to keep the image small
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tini \
        ca-certificates \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Non-root user with a pinned UID, so bind-mounted host paths have predictable
# ownership on Linux hosts. The home directory matters here: boto3 reads AWS
# credentials from $HOME/.aws, which docker-compose mounts read-only.
RUN useradd --create-home --shell /bin/bash --uid 1000 searchagent

WORKDIR /app

# Install deps FIRST so this layer caches across code edits — it only
# invalidates when requirements.txt itself changes.
COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Then the project. Anything in .dockerignore is excluded — notably Reference/,
# which is a different application kept here only for reading.
COPY --chown=searchagent:searchagent . /app/

# Captured runs land here. Mounted as a volume in docker-compose.yml so run
# history survives rebuilds; shared/capture.py writes to a relative "out/" path,
# which resolves to /app/out given WORKDIR.
RUN mkdir -p /app/out && chown -R searchagent:searchagent /app

# Normalise the entrypoint script:
#   - strip a UTF-8 BOM: some Windows editors add one, and the kernel then reads
#     the shebang as "\xEF\xBB\xBF#!/bin/bash" and fails with the misleading
#     "no such file or directory";
#   - strip CRLF: "\r" becomes part of the interpreter path ("/bin/bash\r"),
#     same misleading error;
#   - chmod +x: Git on Windows routinely drops the executable bit.
# All three are no-ops on an already-clean file, so they are safe to keep.
RUN sed -i '1s/^\xEF\xBB\xBF//' /app/docker-entrypoint.sh \
    && sed -i 's/\r$//' /app/docker-entrypoint.sh \
    && chmod +x /app/docker-entrypoint.sh

USER searchagent

# uvicorn listens here inside the container; the host mapping is in
# docker-compose.yml. 8080 rather than 8000 because 8000 is in a Windows
# reserved range on at least one dev machine (winerror 10013).
EXPOSE 8080

# tini reaps signals; the entrypoint ensures /app/out exists and starts uvicorn.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker-entrypoint.sh"]
