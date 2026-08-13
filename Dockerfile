# Stage 1: Build dependencies
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# Enable bytecode compilation and use copy link mode for Docker
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Install dependencies first (separate layer for caching).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra cloud-run

# Install the project itself
COPY README.md ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev --extra cloud-run


# Stage 2: Runtime image
FROM python:3.12-slim-bookworm

WORKDIR /app

ARG SUPERCRONIC_VERSION=0.2.33
# TARGETARCH is set by BuildKit (amd64 / arm64). It matters for the compose
# path: `docker compose up --build` on an Apple Silicon host produces an arm64
# image, and a hardcoded amd64 binary would fail with an exec format error the
# moment ENABLE_DAILY_FETCH is turned on — while the MCP server itself kept
# running, so the scheduled refresh would silently never happen. An unset value
# yields a 404 from curl -f, which fails the build loudly rather than quietly.
ARG TARGETARCH
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && curl -fsSL "https://github.com/aptible/supercronic/releases/download/v${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH}" \
       -o /usr/local/bin/supercronic \
    && chmod +x /usr/local/bin/supercronic \
    && apt-get remove -y --autoremove curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Copy application source and scripts
COPY src/ ./src/
COPY scripts/ ./scripts/

# Make entrypoints executable (entrypoint-compose.sh: self-hosted docker
# compose deployment, the default ENTRYPOINT below; entrypoint-stdio.sh:
# Cloud Run deployment with oauth2-proxy, selected via a --command override
# at deploy time — see jquants-mcp#568)
RUN chmod +x /app/scripts/entrypoint-compose.sh /app/scripts/entrypoint-stdio.sh

# Run as non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Add venv to PATH
ENV PATH="/app/.venv/bin:$PATH"

# Store SQLite cache in /tmp (Cloud Run: cache.db downloaded at startup)
ENV JQUANTS_CACHE_DIR=/tmp

# Unbuffered Python output for Cloud Run logging
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

# Default to the self-hosted compose deployment, so `docker run` on this image
# yields a working MCP server. Cloud Run overrides this with a --command
# pointing at entrypoint-stdio.sh; that entrypoint is the one with the OAuth
# layer, and entrypoint-compose.sh refuses to start when it detects Cloud Run.
ENTRYPOINT ["/app/scripts/entrypoint-compose.sh"]
