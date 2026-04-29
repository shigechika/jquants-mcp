# Stage 1: Build dependencies
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# Enable bytecode compilation and use copy link mode for Docker
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Install dependencies first (separate layer for caching).
# `charts` extra is included in the production image so the
# `render_candlestick` MCP tool is registered (per #109). Adds
# ~120 MB to the image (matplotlib 25 + mplfinance <1 + pandas 48
# + numpy 27 + pillow 14 + transitive deps).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra cloud-run --extra charts

# Install the project itself
COPY README.md ./
COPY src/ ./src/
# hatch-vcs needs git tags; Docker build has no .git dir.
# Fallback: set a placeholder version for the container build.
ENV SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0+docker
RUN uv sync --frozen --no-dev --extra cloud-run --extra charts


# Stage 2: Runtime image
FROM python:3.12-slim-bookworm

WORKDIR /app

# Install Noto CJK JP font so matplotlib can render company names in
# render_candlestick chart titles. ~10 MB; without this Japanese
# characters render as tofu (□).
ARG SUPERCRONIC_VERSION=0.2.33
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk curl \
    && curl -fsSL "https://github.com/aptible/supercronic/releases/download/v${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
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

# Make entrypoint executable
RUN chmod +x /app/scripts/entrypoint.sh

# Run as non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Add venv to PATH
ENV PATH="/app/.venv/bin:$PATH"

# Store SQLite cache in /tmp (Cloud Run: cache.db downloaded at startup)
ENV JQUANTS_CACHE_DIR=/tmp

# Unbuffered Python output for Cloud Run logging
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
