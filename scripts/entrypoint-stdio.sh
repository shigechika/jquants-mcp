#!/bin/bash
# entrypoint-stdio.sh — Docker entrypoint for the mcp-stdio-fronted Cloud Run
# deployment (parallel to entrypoint.sh's streamable-http deployment; see
# jquants-mcp#568 for the migration this is part of).
#
# Workflow:
#   1. Download auth DBs from GCS (small, fast)
#   2. Download cache.db from GCS *synchronously*, before the server starts
#   3. Start supercronic for periodic cache.db re-download (replaces the
#      Pub/Sub-pushed /internal/reload custom_route, which does not exist on
#      a stdio-only server — there is no HTTP route for mcp-stdio to forward
#      a push to, so this service polls GCS on a timer instead)
#   4. Start mcp-stdio serve (fronts the stdio jquants-mcp child over HTTP;
#      oauth2-proxy sits in front of this as a Cloud Run sidecar and is not
#      started here)
#   5. Start background GCS sync daemon (users.db + oauth_state.db upload only)
#   6. On SIGTERM: stop mcp-stdio serve, stop supercronic, stop the daemon
#      (triggers its final GCS upload)
#
# Why the cache.db download is synchronous (Step 2): under Cloud Run
# request-based billing the CPU is throttled to ~0 between requests, so a
# background download started *after* the server is ready is CPU-starved and
# never finishes — the instance scales to zero with the download incomplete.
# The container-startup window has full CPU (plus --cpu-boost), so we download
# here, before binding the port. A failure is non-fatal: the server falls back
# to the live J-Quants API.
set -euo pipefail

PORT="${PORT:-8081}"
PUBLIC_URL="${PUBLIC_URL:?PUBLIC_URL must be set (e.g. https://jquants.aikawa.jp)}"

echo "=== jquants-mcp (stdio/serve) startup ==="
echo "PORT=${PORT}"
echo "PUBLIC_URL=${PUBLIC_URL}"
echo "GCS_BUCKET=${GCS_BUCKET:-<not set>}"
echo "JQUANTS_CACHE_DIR=${JQUANTS_CACHE_DIR:-/tmp}"

if [ -n "${GCS_BUCKET:-}" ]; then
    # Step 1: Download auth databases from GCS (small, needed for auth)
    echo "Downloading auth databases from GCS..."
    # Non-fatal, same rationale as entrypoint.sh: a missing object on first
    # run is not a failure (exits 0); a genuine download failure must not
    # abort startup under `set -e` — the server can still run with
    # local/empty auth state.
    python /app/scripts/gcs_sync.py --init \
        || echo "WARNING: auth DB download failed; continuing with local state"

    # Step 2: Download cache.db from GCS *synchronously* (see header note).
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

    if [ -n "${GCS_DAEMON_PID:-}" ]; then
        echo "Stopping GCS sync daemon (PID=${GCS_DAEMON_PID})..."
        kill -TERM "${GCS_DAEMON_PID}" 2>/dev/null || true
        wait "${GCS_DAEMON_PID}" 2>/dev/null || true
    fi

    echo "Shutdown complete"
    exit 0
}

trap _shutdown SIGTERM SIGINT

# Step 4: Start supercronic for periodic cache.db re-download (opt-in via the
# same GCS_BUCKET gate as the synchronous download above — nothing to poll
# without a bucket). REQUIRES the service to be deployed with
# --no-cpu-throttling (CPU always allocated) — see cache-poll.crontab's
# header for why request-based throttling breaks this.
if [ -n "${GCS_BUCKET:-}" ]; then
    echo "Starting supercronic for periodic cache.db re-download..."
    supercronic /app/scripts/cache-poll.crontab &
    SUPERCRONIC_PID=$!
    echo "supercronic started (PID=${SUPERCRONIC_PID})"
fi

# Step 5: Start mcp-stdio serve (cache.db already downloaded in Step 2, or
# live-API fallback if the download was skipped/failed). oauth2-proxy is a
# separate sidecar container in front of this one on Cloud Run; it is not
# started here.
echo "Starting mcp-stdio serve on port ${PORT}..."
# --token-store-firestore (mcp-stdio 0.42.0+, mcp-stdio#404): without it,
# every instance restart/redeploy invalidates all issued OAuth tokens and
# forces every connected client to re-authenticate. mcp-stdio's own --help
# warns there is no lock against two processes sharing one document -- this
# service's min/max-instances=1 bounds *steady-state* concurrency, but does
# NOT prevent the outgoing and incoming instance from overlapping briefly
# during a normal Cloud Run redeploy or instance recycle (there is no
# "drain fully, then start" mode on Cloud Run). A write race in that window
# can silently drop one instance's token issuance, forcing that one client
# to re-authenticate -- strictly no worse than today's in-memory-only
# baseline (which invalidates every token on every restart), but not fully
# eliminated by this flag alone. Tracked upstream: mcp-stdio#406
# (transactional/generation-checked writes would close this).
mcp-stdio serve \
    --enable-oauth \
    --public-url "${PUBLIC_URL}" \
    --path /mcp \
    --trusted-user-header X-Forwarded-Email \
    --user-env JQUANTS_MCP_USER \
    --access-token-ttl 86400 \
    --max-sessions 8 \
    --max-sessions-per-owner 6 \
    --modern-idle-ttl 600 \
    --session-idle-ttl 1800 \
    --token-store-firestore "${FIRESTORE_TOKEN_STORE:-mcp_stdio_oauth/state}" \
    --allow-redirect-uri https://claude.ai/api/mcp/auth_callback \
    --host 127.0.0.1 \
    --port "${PORT}" \
    -- jquants-mcp &
SERVE_PID=$!
echo "mcp-stdio serve started (PID=${SERVE_PID})"

# Step 6: Start GCS sync daemon (uploads users.db + oauth_state.db only)
if [ -n "${GCS_BUCKET:-}" ]; then
    echo "Starting GCS sync daemon..."
    python /app/scripts/gcs_sync.py --daemon &
    GCS_DAEMON_PID=$!
    echo "GCS sync daemon started (PID=${GCS_DAEMON_PID})"
fi

# Wait for mcp-stdio serve to exit
wait "${SERVE_PID}"
SERVE_EXIT=$?
echo "mcp-stdio serve exited with code ${SERVE_EXIT}"

# If serve exited on its own (not via SIGTERM), stop the other background
# processes and exit with the same code.
if [ -n "${SUPERCRONIC_PID:-}" ]; then
    kill -TERM "${SUPERCRONIC_PID}" 2>/dev/null || true
    wait "${SUPERCRONIC_PID}" 2>/dev/null || true
fi
if [ -n "${GCS_DAEMON_PID:-}" ]; then
    kill -TERM "${GCS_DAEMON_PID}" 2>/dev/null || true
    wait "${GCS_DAEMON_PID}" 2>/dev/null || true
fi

exit "${SERVE_EXIT}"
