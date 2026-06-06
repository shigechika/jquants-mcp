#!/bin/bash
# entrypoint.sh — Docker entrypoint for Cloud Run deployment.
#
# Workflow:
#   1. Download auth DBs from GCS (small, fast)
#   2. Download cache.db from GCS *synchronously*, before the server starts
#   3. Start MCP server (serves from cache.db, or the live API if it is missing)
#   4. Start background GCS sync daemon (users.db + oauth_state.db upload only)
#   5. On SIGTERM: stop MCP server, stop daemon (triggers final GCS upload)
#
# Why the cache.db download is synchronous (Step 2): under Cloud Run
# request-based billing the CPU is throttled to ~0 between requests, so a
# background download started *after* the server is ready is CPU-starved and
# never finishes — the instance scales to zero with the download incomplete.
# The container-startup window has full CPU (plus --cpu-boost), so we download
# here, before binding the port. A failure is non-fatal: the server falls back
# to the live J-Quants API.
set -euo pipefail

PORT="${PORT:-8000}"
ENABLE_DAILY_FETCH="${ENABLE_DAILY_FETCH:-}"

echo "=== jquants-mcp startup ==="
echo "PORT=${PORT}"
echo "GCS_BUCKET=${GCS_BUCKET:-<not set>}"
echo "JQUANTS_CACHE_DIR=${JQUANTS_CACHE_DIR:-/tmp}"
echo "ENABLE_DAILY_FETCH=${ENABLE_DAILY_FETCH:-false}"

if [ -n "${GCS_BUCKET:-}" ]; then
    # Step 1: Download auth databases from GCS (small, needed for auth)
    echo "Downloading auth databases from GCS..."
    # Non-fatal: gcs_sync now exits non-zero on a genuine download failure (for
    # cron/manual detection), but startup must continue under `set -e` — the
    # server can still run with local/empty auth state. A missing object on
    # first run is not a failure and exits 0.
    python /app/scripts/gcs_sync.py --init \
        || echo "WARNING: auth DB download failed; continuing with local state"

    # Step 2: Download cache.db from GCS *synchronously*, before the server
    # starts (see the header note on request-based billing). Non-fatal — on
    # failure the server serves via the live J-Quants API (gcs_sync.py also
    # logs "cache.db download failed", the phrase the download alert greps for).
    echo "Downloading cache.db from GCS (synchronous startup)..."
    if python /app/scripts/gcs_sync.py --init-cache; then
        echo "cache.db download complete"
    else
        echo "cache.db download failed; continuing with live-API fallback"
    fi
else
    echo "GCS_BUCKET not set, skipping GCS downloads"
fi

# Step 3: SIGTERM / SIGINT handler
GCS_DAEMON_PID=""
MCP_PID=""
SUPERCRONIC_PID=""

_shutdown() {
    echo "Received shutdown signal"

    # Stop MCP server
    if [ -n "${MCP_PID:-}" ]; then
        echo "Stopping MCP server (PID=${MCP_PID})..."
        kill -TERM "${MCP_PID}" 2>/dev/null || true
        wait "${MCP_PID}" 2>/dev/null || true
    fi

    # Stop GCS daemon (triggers final upload via its own SIGTERM handler)
    if [ -n "${GCS_DAEMON_PID:-}" ]; then
        echo "Stopping GCS sync daemon (PID=${GCS_DAEMON_PID})..."
        kill -TERM "${GCS_DAEMON_PID}" 2>/dev/null || true
        wait "${GCS_DAEMON_PID}" 2>/dev/null || true
    fi

    # Stop supercronic if running
    if [ -n "${SUPERCRONIC_PID:-}" ]; then
        echo "Stopping supercronic (PID=${SUPERCRONIC_PID})..."
        kill -TERM "${SUPERCRONIC_PID}" 2>/dev/null || true
        wait "${SUPERCRONIC_PID}" 2>/dev/null || true
    fi

    echo "Shutdown complete"
    exit 0
}

trap _shutdown SIGTERM SIGINT

# Step 4: Start MCP server (cache.db already downloaded in Step 2, or live-API
# fallback if the download was skipped/failed).
echo "Starting MCP server on port ${PORT}..."
jquants-mcp --transport streamable-http --host 0.0.0.0 --port "${PORT}" &
MCP_PID=$!
echo "MCP server started (PID=${MCP_PID})"

# Step 4b: Start supercronic for scheduled daily fetch (opt-in)
if [ "${ENABLE_DAILY_FETCH}" = "true" ] || [ "${ENABLE_DAILY_FETCH}" = "1" ]; then
    echo "Starting supercronic for daily cache fetch..."
    supercronic /app/scripts/daily-fetch.crontab &
    SUPERCRONIC_PID=$!
    echo "supercronic started (PID=${SUPERCRONIC_PID})"
fi

# Step 5: Start GCS sync daemon (uploads users.db + oauth_state.db only)
if [ -n "${GCS_BUCKET:-}" ]; then
    echo "Starting GCS sync daemon..."
    python /app/scripts/gcs_sync.py --daemon &
    GCS_DAEMON_PID=$!
    echo "GCS sync daemon started (PID=${GCS_DAEMON_PID})"
fi

# Wait for MCP server to exit
wait "${MCP_PID}"
MCP_EXIT=$?
echo "MCP server exited with code ${MCP_EXIT}"

# If MCP server exited on its own (not via SIGTERM), stop the daemon and exit
if [ -n "${GCS_DAEMON_PID:-}" ]; then
    kill -TERM "${GCS_DAEMON_PID}" 2>/dev/null || true
    wait "${GCS_DAEMON_PID}" 2>/dev/null || true
fi
if [ -n "${SUPERCRONIC_PID:-}" ]; then
    kill -TERM "${SUPERCRONIC_PID}" 2>/dev/null || true
    wait "${SUPERCRONIC_PID}" 2>/dev/null || true
fi

exit "${MCP_EXIT}"
