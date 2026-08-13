#!/bin/bash
# entrypoint-compose.sh — Docker entrypoint for the self-hosted compose
# deployment (compose.yml). This is the image's default ENTRYPOINT.
#
# It fronts the stdio-only server over HTTP with `mcp-stdio serve`, so an MCP
# client can talk to http://localhost:8080/mcp without the operator installing
# Python, uv, or a reverse proxy. Replaces the streamable-http entrypoint that
# broke when the server moved to the official MCP SDK (#601).
#
# Workflow:
#   1. Refuse to run on Cloud Run (see the guard below)
#   2. Optionally start supercronic for the scheduled cache fetch
#   3. Start mcp-stdio serve, which spawns jquants-mcp as a stdio child
#   4. On SIGTERM: stop serve, then supercronic
#
# Authentication: none by default. `mcp-stdio serve` reads
# MCP_STDIO_SERVE_TOKEN from the environment and, when it is set, requires it
# as a bearer token on MCP requests. The env var is used rather than
# --auth-token because a flag value is visible in `ps`. compose.yml publishes
# the port on 127.0.0.1 only, so the default no-auth setup is not reachable
# from off-host; set the token before changing that.
#
# Cache: there is no GCS in this deployment. The cache.db in the mounted
# volume starts empty and fills as tools are called — anything not cached
# falls through to the live J-Quants API, so a fresh install works
# immediately, just without the speed of a warm cache. Set
# ENABLE_DAILY_FETCH=true to keep it warm on a schedule.
set -euo pipefail

PORT="${PORT:-8080}"
ENABLE_DAILY_FETCH="${ENABLE_DAILY_FETCH:-}"

# Accept the bearer token from a file as well as from the environment, using
# the *_FILE convention the postgres/mysql images established. A value passed
# through `environment:` is baked into the container config and shows up in
# `docker inspect`; a compose `secrets:` entry is a file, and this is what lets
# one be used. Only worth bothering with when the endpoint is exposed beyond
# localhost — see compose.yml.
if [ -n "${MCP_STDIO_SERVE_TOKEN_FILE:-}" ]; then
    if [ ! -r "${MCP_STDIO_SERVE_TOKEN_FILE}" ]; then
        echo "FATAL: MCP_STDIO_SERVE_TOKEN_FILE=${MCP_STDIO_SERVE_TOKEN_FILE} is not readable." >&2
        echo "       Refusing to start: falling back to no authentication would be worse." >&2
        exit 1
    fi
    MCP_STDIO_SERVE_TOKEN="$(cat "${MCP_STDIO_SERVE_TOKEN_FILE}")"
    # An empty file would export an empty token, which serve treats as "no
    # --auth-token given" — i.e. exactly the silent fallback to no
    # authentication that the readability check above refuses. Same treatment.
    if [ -z "${MCP_STDIO_SERVE_TOKEN}" ]; then
        echo "FATAL: MCP_STDIO_SERVE_TOKEN_FILE=${MCP_STDIO_SERVE_TOKEN_FILE} is empty." >&2
        echo "       Refusing to start: an empty token means no authentication." >&2
        exit 1
    fi
    export MCP_STDIO_SERVE_TOKEN
fi

# This entrypoint carries no OAuth layer and no identity plumbing: it does not
# pass --enable-oauth or --user-env, so the child runs single-user against
# JQUANTS_API_KEY. On Cloud Run the authentication lives in the oauth2-proxy
# sidecar, whose skip-auth routes deliberately pass /mcp straight through to
# the app container on the assumption that the app enforces its own OAuth.
# Booting this entrypoint there would therefore publish an unauthenticated
# /mcp. Cloud Run selects scripts/entrypoint-stdio.sh via a --command override
# on the service; if that override is ever lost, fail loudly here rather than
# silently serving without authentication. K_SERVICE is set by Cloud Run.
if [ -n "${K_SERVICE:-}" ]; then
    echo "FATAL: entrypoint-compose.sh is for self-hosted docker compose only." >&2
    echo "       Cloud Run must run scripts/entrypoint-stdio.sh (--command override)." >&2
    echo "       Refusing to start: this entrypoint has no authentication layer." >&2
    exit 1
fi

echo "=== jquants-mcp (compose) startup ==="
echo "PORT=${PORT}"
echo "JQUANTS_CACHE_DIR=${JQUANTS_CACHE_DIR:-/tmp}"
echo "ENABLE_DAILY_FETCH=${ENABLE_DAILY_FETCH:-false}"
if [ -n "${MCP_STDIO_SERVE_TOKEN:-}" ]; then
    echo "auth: bearer token required (MCP_STDIO_SERVE_TOKEN is set)"
else
    echo "auth: none — the port is published on 127.0.0.1 only by compose.yml"
fi

SERVE_PID=""
SUPERCRONIC_PID=""

_shutdown() {
    echo "Received shutdown signal"

    if [ -n "${SERVE_PID:-}" ]; then
        echo "Stopping mcp-stdio serve (PID=${SERVE_PID})..."
        kill -TERM "${SERVE_PID}" 2>/dev/null || true
        wait "${SERVE_PID}" 2>/dev/null || true
    fi

    if [ -n "${SUPERCRONIC_PID:-}" ]; then
        echo "Stopping supercronic (PID=${SUPERCRONIC_PID})..."
        kill -TERM "${SUPERCRONIC_PID}" 2>/dev/null || true
        wait "${SUPERCRONIC_PID}" 2>/dev/null || true
    fi

    echo "Shutdown complete"
    exit 0
}

trap _shutdown SIGTERM SIGINT

# Scheduled cache fetch (opt-in), unchanged from the entrypoint this replaces.
if [ "${ENABLE_DAILY_FETCH}" = "true" ] || [ "${ENABLE_DAILY_FETCH}" = "1" ]; then
    echo "Starting supercronic for daily cache fetch..."
    supercronic /app/scripts/daily-fetch.crontab &
    SUPERCRONIC_PID=$!
    echo "supercronic started (PID=${SUPERCRONIC_PID})"
fi

# Bind 0.0.0.0 inside the container: the container's loopback is its own, so
# binding 127.0.0.1 here would make the published port unreachable. compose.yml
# is what restricts exposure, by publishing to 127.0.0.1 on the host.
echo "Starting mcp-stdio serve on port ${PORT}..."
mcp-stdio serve \
    --host 0.0.0.0 \
    --port "${PORT}" \
    --path /mcp \
    -- jquants-mcp &
SERVE_PID=$!
echo "mcp-stdio serve started (PID=${SERVE_PID})"

# `|| SERVE_EXIT=$?` rather than a bare `wait`: under `set -e` a non-zero wait
# terminates the script right here, so the exit-code log and the supercronic
# cleanup below would never run when the gateway fails (a bad PORT, say).
SERVE_EXIT=0
wait "${SERVE_PID}" || SERVE_EXIT=$?
echo "mcp-stdio serve exited with code ${SERVE_EXIT}"

if [ -n "${SUPERCRONIC_PID:-}" ]; then
    kill -TERM "${SUPERCRONIC_PID}" 2>/dev/null || true
    wait "${SUPERCRONIC_PID}" 2>/dev/null || true
fi

exit "${SERVE_EXIT}"
