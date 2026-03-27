"""FastMCP server definition and tool registration."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from . import __version__
from .cache.store import CacheStore
from .client import JQuantsClient
from .config import Settings

logger = logging.getLogger(__name__)

# Paths that trigger OAuth debug logging
_OAUTH_DEBUG_PATHS = ("/oauth/", "/.well-known/")


class OAuthDebugMiddleware(BaseHTTPMiddleware):
    """Log OAuth-related HTTP requests to help diagnose auth flow issues."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        path = request.url.path
        is_oauth = any(p in path for p in _OAUTH_DEBUG_PATHS)

        if is_oauth:
            query = dict(request.query_params)
            # Log sanitized headers (omit Authorization value)
            headers = {
                k: (v if k.lower() != "authorization" else "[REDACTED]")
                for k, v in request.headers.items()
            }
            logger.info(
                "OAuth request: method=%s path=%s query=%r headers=%r",
                request.method,
                path,
                query,
                headers,
            )

        response = await call_next(request)

        if is_oauth:
            logger.info(
                "OAuth response: method=%s path=%s status=%d",
                request.method,
                path,
                response.status_code,
            )

        return response


mcp = FastMCP("jquants-dat-mcp")

# Shared globals — initialized lazily at first request
_settings: Settings | None = None
_cache: CacheStore | None = None

# Single-user global client (bearer-token / no-auth mode)
_client: JQuantsClient | None = None

# Multi-user client pool: user_id → JQuantsClient (one per authenticated user)
_user_clients: dict[str, JQuantsClient] = {}

# Last-used timestamps for stale client eviction: user_id → monotonic timestamp
_user_client_last_used: dict[str, float] = {}

# Timestamp of the last stale-client cleanup pass (monotonic)
_last_cleanup: float = 0.0

# Run cleanup at most once every 5 minutes
_CLEANUP_INTERVAL = 300

# UserStore — lazily initialized when encryption_key is configured
_user_db = None  # UserStore | None


def _get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def _get_client() -> JQuantsClient:
    global _client
    if _client is None:
        _client = JQuantsClient(_get_settings())
    return _client


def _get_cache() -> CacheStore:
    global _cache
    if _cache is None:
        settings = _get_settings()
        db_path = settings.get_cache_dir() / "cache.db"
        _cache = CacheStore(db_path, default_plan=settings.jquants_plan)
    return _cache


def _get_user_db():
    """Return the UserStore, creating it if encryption is configured.

    Returns None when no encryption_key is set (single-user mode).
    """
    global _user_db
    if _user_db is not None:
        return _user_db

    settings = _get_settings()
    if not settings.encryption_key:
        return None

    from .crypto import decrypt, encrypt
    from .db.users import UserStore

    # Pass passphrase directly — encrypt/decrypt now handle salt derivation internally
    passphrase = settings.encryption_key
    db_path = settings.get_cache_dir() / "users.db"
    _user_db = UserStore(
        db_path,
        encrypt_fn=lambda pt: encrypt(pt, passphrase),
        decrypt_fn=lambda blob: decrypt(blob, passphrase),
    )
    return _user_db


async def _evict_stale_clients() -> None:
    """Evict in-memory client instances that have been idle for more than 1 hour."""
    from .validation import _STALE_CLIENT_TTL

    now = time.monotonic()
    stale = [uid for uid, ts in _user_client_last_used.items() if now - ts > _STALE_CLIENT_TTL]
    for uid in stale:
        client = _user_clients.pop(uid, None)
        _user_client_last_used.pop(uid, None)
        if client is not None:
            await client.close()
        logger.info("Evicted stale client for user %s (idle >%ds)", uid, _STALE_CLIENT_TTL)


async def _get_user_client() -> JQuantsClient:
    """Return the J-Quants client for the currently authenticated user.

    Resolution order:
    1. No auth / bearer-token auth → global single-user client (settings.jquants_api_key)
    2. OAuth user without encryption_key configured → global client (shared)
    3. OAuth user with encryption_key → per-user client from UserStore

    Performs daily API key validation and stale client cleanup as side effects.

    Raises:
        UserNotConfiguredError: When multi-user mode is active and the current
            user has not yet registered their J-Quants API key.
        InvalidAPIKeyError: When daily validation detects that the stored API key
            has been revoked.
    """
    global _last_cleanup

    from fastmcp.server.dependencies import get_access_token

    from .exceptions import InvalidAPIKeyError, UserNotConfiguredError

    token = get_access_token()

    # No auth or static bearer token → use global client
    if token is None or token.client_id == "bearer":
        return _get_client()

    user_id = token.client_id
    user_db = _get_user_db()

    # encryption_key not configured → share the global client for all OAuth users
    if user_db is None:
        return _get_client()

    # Periodically evict stale clients
    now_mono = time.monotonic()
    if now_mono - _last_cleanup > _CLEANUP_INTERVAL:
        await _evict_stale_clients()
        _last_cleanup = now_mono

    # Look up the user's API key from the encrypted store
    from .exceptions import DecryptionError

    user = user_db.get_user(user_id)
    if user is None:
        if user_db.has_corrupted_key(user_id):
            # Key exists in DB but failed to decrypt — provide actionable error
            raise DecryptionError()
        raise UserNotConfiguredError(user_id)

    # Build per-user client if not yet cached
    if user_id not in _user_clients:
        user_settings = Settings(
            jquants_api_key=user.api_key,
            jquants_plan=user.plan,
        )
        _user_clients[user_id] = JQuantsClient(user_settings)

    client = _user_clients[user_id]
    _user_client_last_used[user_id] = now_mono

    # Daily API key validation
    from .validation import needs_validation, validate_api_key
    from .exceptions import AuthenticationError

    if needs_validation(user.last_validated_at):
        try:
            await validate_api_key(client)
            user_db.update_last_validated(user_id)
            logger.info("Daily validation passed for user %s", user_id)
        except AuthenticationError:
            # Key is no longer valid — evict cached client and surface error
            _user_clients.pop(user_id, None)
            _user_client_last_used.pop(user_id, None)
            raise InvalidAPIKeyError(user_id)

    return client


# ------------------------------------------------------------------
# Utility tools
# ------------------------------------------------------------------


@mcp.tool()
def health_check() -> dict[str, Any]:
    """Check server health and API key configuration.

    Returns server version, API key status, and active plan.
    In multi-user mode, returns the authenticated user's plan
    instead of the global default.
    """
    from fastmcp.server.dependencies import get_access_token

    settings = _get_settings()
    has_key = bool(settings.jquants_api_key)
    plan = settings.jquants_plan

    # In multi-user mode, resolve the actual user's plan
    token = get_access_token()
    if token is not None and token.client_id != "bearer":
        user_db = _get_user_db()
        if user_db is not None:
            user = user_db.get_user(token.client_id)
            if user is not None:
                plan = user.plan
                has_key = True

    return {
        "status": "healthy",
        "service": "jquants-dat-mcp",
        "version": __version__,
        "api_key_configured": has_key,
        "plan": plan,
    }


@mcp.tool()
def cache_status() -> dict[str, Any]:
    """Show cache statistics.

    Returns per-table row counts and database file size.
    """
    return _get_cache().status()


@mcp.tool()
def cache_clear(table: str | None = None) -> dict[str, Any]:
    """Clear cached data.

    Args:
        table: Table name to clear. Clears all tables when omitted.
    """
    result = _get_cache().clear(table)
    return {"cleared": result}


@mcp.tool()
async def register_api_key(
    api_key: str,
    plan: str = "free",
) -> dict[str, Any]:
    """Register or update your J-Quants API key (multi-user mode).

    Stores your J-Quants API key encrypted in the server's user database,
    associated with your OAuth identity. Subsequent tool calls will
    automatically use this key.

    For security, registering via the browser at /settings is recommended
    instead of passing the API key through this tool.

    This tool requires OAuth 2.1 authentication and server-side encryption
    (MCP_ENCRYPTION_KEY) to be configured.

    Args:
        api_key: Your J-Quants API key (refresh token from the J-Quants portal).
        plan: Your J-Quants plan (free | light | standard | premium). Affects rate limits.
    """
    from fastmcp.server.dependencies import get_access_token

    from .models.user import User

    token = get_access_token()
    if token is None or token.client_id == "bearer":
        return {
            "error": True,
            "message": "register_api_key requires OAuth 2.1 authentication.",
        }

    user_db = _get_user_db()
    if user_db is None:
        return {
            "error": True,
            "message": (
                "Multi-user mode is not enabled. "
                "Set MCP_ENCRYPTION_KEY on the server to enable per-user API key storage."
            ),
        }

    valid_plans = {"free", "light", "standard", "premium"}
    if plan not in valid_plans:
        return {
            "error": True,
            "message": f"Invalid plan '{plan}'. Must be one of: {', '.join(sorted(valid_plans))}",
        }

    user_id = token.client_id
    user = User(user_id=user_id, api_key=api_key, plan=plan)
    user_db.save_user(user)

    # Invalidate the cached client so the next call picks up the new key
    _user_clients.pop(user_id, None)
    _user_client_last_used.pop(user_id, None)

    # Probe plan-specific endpoints to verify / auto-detect the actual plan
    from .audit import audit
    from .config import Settings as _Settings
    from .validation import detect_plan

    probe_client = JQuantsClient(_Settings(jquants_api_key=api_key, jquants_plan=plan))
    warnings: list[str] = []
    try:
        detected_plan = await detect_plan(probe_client)
        if detected_plan != plan:
            user_db.update_plan(user_id, detected_plan)
            warnings.append(
                f"Claimed plan '{plan}' differs from detected plan '{detected_plan}'. "
                f"Stored plan updated to '{detected_plan}'."
            )
            plan = detected_plan
    except Exception as e:
        logger.warning("Plan detection failed during registration for user %s: %s", user_id, e)
        warnings.append(f"Plan detection skipped due to error: {e}")

    audit("register_api_key", user_id=user_id, plan=plan)

    result: dict[str, Any] = {
        "status": "ok",
        "user_id": user_id,
        "plan": plan,
        "message": "API key registered successfully.",
    }
    if warnings:
        result["warnings"] = warnings
    return result


@mcp.tool()
async def delete_api_key() -> dict[str, Any]:
    """Delete your registered J-Quants API key (multi-user mode).

    Removes your API key from the server. Subsequent tool calls will fail
    until you register a new key with register_api_key.

    This tool requires OAuth 2.1 authentication.
    """
    from fastmcp.server.dependencies import get_access_token

    token = get_access_token()
    if token is None or token.client_id == "bearer":
        return {
            "error": True,
            "message": "delete_api_key requires OAuth 2.1 authentication.",
        }

    user_db = _get_user_db()
    if user_db is None:
        return {
            "error": True,
            "message": "Multi-user mode is not enabled (MCP_ENCRYPTION_KEY not set).",
        }

    from .audit import audit

    user_id = token.client_id
    deleted = user_db.delete_user(user_id)
    _user_clients.pop(user_id, None)

    if deleted:
        audit("delete_api_key", user_id=user_id)
        return {"status": "ok", "message": "API key deleted."}
    return {"status": "not_found", "message": "No API key was registered for this user."}


# ------------------------------------------------------------------
# Tool registration
# ------------------------------------------------------------------


def _register_tools() -> None:
    """Register all endpoint tools. Called during module import."""
    from .tools import bulk, derivatives, equities, financials, indices, markets

    equities.register(mcp, _get_user_client, _get_cache)
    financials.register(mcp, _get_user_client, _get_cache)
    indices.register(mcp, _get_user_client, _get_cache)
    derivatives.register(mcp, _get_user_client, _get_cache)
    markets.register(mcp, _get_user_client, _get_cache)
    bulk.register(mcp, _get_user_client, _get_cache)


_register_tools()

from .settings_ui import register_settings_routes  # noqa: E402

register_settings_routes(mcp, _get_user_db, _user_clients, _user_client_last_used, _get_settings)


# ------------------------------------------------------------------
# Server startup
# ------------------------------------------------------------------


def run_server(
    transport: str = "stdio",
    host: str = "0.0.0.0",
    port: int = 8080,
    ssl_certfile: str = "",
    ssl_keyfile: str = "",
    bearer_token: str = "",
    github_client_id: str = "",
    github_client_secret: str = "",
    oauth_base_url: str = "",
) -> None:
    """Start the MCP server.

    Args:
        transport: Transport type ("stdio" or "streamable-http")
        host: Bind address for HTTP transport
        port: Port number for HTTP transport
        ssl_certfile: Path to SSL certificate file
        ssl_keyfile: Path to SSL private key file
        bearer_token: Bearer token for authentication (fallback if OAuth not configured)
        github_client_id: GitHub OAuth App client ID (enables OAuth 2.1)
        github_client_secret: GitHub OAuth App client secret
        oauth_base_url: Public base URL for OAuth endpoints (e.g. https://mcp.example.com)
    """
    logging.basicConfig(level=logging.INFO, format="%(name)s - %(levelname)s - %(message)s")
    logger.info("jquants-dat-mcp v%s starting (transport=%s)", __version__, transport)

    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        # Apply CLI overrides to settings before creating auth provider
        settings = _get_settings()
        ssl_certfile = ssl_certfile or settings.ssl_certfile
        ssl_keyfile = ssl_keyfile or settings.ssl_keyfile

        # CLI overrides take precedence over config for OAuth/Bearer settings
        if bearer_token:
            settings.bearer_token = bearer_token
        if github_client_id:
            settings.github_client_id = github_client_id
        if github_client_secret:
            settings.github_client_secret = github_client_secret
        if oauth_base_url:
            settings.oauth_base_url = oauth_base_url

        # Configure authentication
        from .auth import create_auth_provider

        auth_provider = create_auth_provider(settings)
        if auth_provider is not None:
            mcp.auth = auth_provider

        # TLS configuration
        uvicorn_config: dict[str, Any] = {}
        if ssl_certfile and ssl_keyfile:
            uvicorn_config["ssl_certfile"] = ssl_certfile
            uvicorn_config["ssl_keyfile"] = ssl_keyfile
            scheme = "https"
        else:
            scheme = "http"

        logger.info("%s server: %s://%s:%d/mcp", transport, scheme, host, port)
        debug_middleware = [Middleware(OAuthDebugMiddleware)]
        mcp.run(
            transport=transport,
            host=host,
            port=port,
            uvicorn_config=uvicorn_config,
            middleware=debug_middleware,
        )
