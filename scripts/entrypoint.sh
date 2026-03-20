#!/bin/bash
# entrypoint.sh — Docker entrypoint for Cloud Run deployment.
#
# Workflow:
#   1. Download cache files from GCS to /tmp
#   2. Start background GCS sync daemon
#   3. Start MCP server (streamable-http)
#   4. On SIGTERM: stop MCP server, stop daemon (triggers final GCS upload)
set -euo pipefail

PORT="${PORT:-8000}"

echo "=== jquants-dat-mcp startup ==="
echo "PORT=${PORT}"
echo "GCS_BUCKET=${GCS_BUCKET:-<not set>}"
echo "JQUANTS_CACHE_DIR=${JQUANTS_CACHE_DIR:-/tmp}"

# Step 1: Download cache from GCS
if [ -n "${GCS_BUCKET:-}" ]; then
    echo "Downloading cache from GCS..."
    python /app/scripts/gcs_sync.py --init
else
    echo "GCS_BUCKET not set, skipping GCS download"
fi

# Step 2: Start background GCS sync daemon
if [ -n "${GCS_BUCKET:-}" ]; then
    echo "Starting GCS sync daemon..."
    python /app/scripts/gcs_sync.py --daemon &
    GCS_DAEMON_PID=$!
    echo "GCS sync daemon started (PID=${GCS_DAEMON_PID})"
else
    GCS_DAEMON_PID=""
fi

# Step 3: SIGTERM / SIGINT handler
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

# Step 4: Start MCP server in the background
echo "Starting MCP server on port ${PORT}..."
jquants-dat-mcp --transport streamable-http --host 0.0.0.0 --port "${PORT}" &
MCP_PID=$!
echo "MCP server started (PID=${MCP_PID})"

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
