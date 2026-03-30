#!/bin/bash
# entrypoint.sh — Docker entrypoint for Cloud Run deployment.
#
# Workflow:
#   1. Download cache.db from GCS to /tmp (startup copy)
#   2. Download small DB files from GCS (users.db, oauth_state.db)
#   3. Start MCP server (streamable-http)
#   4. Start background GCS sync daemon (users.db + oauth_state.db upload only)
#   5. On SIGTERM: stop MCP server, stop daemon (triggers final GCS upload)
set -euo pipefail

PORT="${PORT:-8000}"

echo "=== jquants-dat-mcp startup ==="
echo "PORT=${PORT}"
echo "GCS_BUCKET=${GCS_BUCKET:-<not set>}"
echo "JQUANTS_CACHE_DIR=${JQUANTS_CACHE_DIR:-/tmp}"

# Step 1: Download cache.db from GCS (大きいので先にコピー)
if [ -n "${GCS_BUCKET:-}" ]; then
    CACHE_SRC="gs://${GCS_BUCKET}/jquants-dat-mcp/cache.db"
    CACHE_DST="${JQUANTS_CACHE_DIR:-/tmp}/cache.db"
    echo "Downloading cache.db from ${CACHE_SRC}..."
    if gcloud storage cp "${CACHE_SRC}" "${CACHE_DST}" 2>&1; then
        echo "cache.db ready: $(du -h "${CACHE_DST}" | cut -f1)"
    else
        echo "WARNING: cache.db download failed, server will start without cache"
    fi
else
    echo "GCS_BUCKET not set, skipping cache.db download"
fi

# Step 2: Download small auth files from GCS (fast, needed for auth)
if [ -n "${GCS_BUCKET:-}" ]; then
    echo "Downloading auth databases from GCS..."
    python /app/scripts/gcs_sync.py --init
else
    echo "GCS_BUCKET not set, skipping GCS download"
fi

# Step 3: SIGTERM / SIGINT handler
GCS_DAEMON_PID=""
MCP_PID=""

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

    echo "Shutdown complete"
    exit 0
}

trap _shutdown SIGTERM SIGINT

# Step 4: Start MCP server (cache.db is already in /tmp)
echo "Starting MCP server on port ${PORT}..."
jquants-dat-mcp --transport streamable-http --host 0.0.0.0 --port "${PORT}" &
MCP_PID=$!
echo "MCP server started (PID=${MCP_PID})"

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

# If MCP server exited on its own (not via SIGTERM), stop daemon and exit
if [ -n "${GCS_DAEMON_PID:-}" ]; then
    kill -TERM "${GCS_DAEMON_PID}" 2>/dev/null || true
    wait "${GCS_DAEMON_PID}" 2>/dev/null || true
fi

exit "${MCP_EXIT}"
