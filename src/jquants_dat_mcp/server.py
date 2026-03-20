"""FastMCP server definition and tool registration."""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP

from . import __version__
from .cache.store import CacheStore
from .client import JQuantsClient
from .config import Settings

logger = logging.getLogger(__name__)

mcp = FastMCP("jquants-dat-mcp")

# Shared globals — initialized lazily at first request
_settings: Settings | None = None
_cache: CacheStore | None = None

# Single-user global client (bearer-token / no-auth mode)
_client: JQuantsClient | None = None

# Multi-user client pool: user_id → JQuantsClient (one per authenticated user)
_user_clients: dict[str, JQuantsClient] = {}

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

    from .crypto import decrypt, derive_key, encrypt
    from .db.users import UserStore

    key = derive_key(settings.encryption_key)
    db_path = settings.get_cache_dir() / "users.db"
    _user_db = UserStore(
        db_path,
        encrypt_fn=lambda pt: encrypt(pt, key),
        decrypt_fn=lambda blob: decrypt(blob, key),
    )
    return _user_db


async def _get_user_client() -> JQuantsClient:
    """Return the J-Quants client for the currently authenticated user.

    Resolution order:
    1. No auth / bearer-token auth → global single-user client (settings.jquants_api_key)
    2. OAuth user without encryption_key configured → global client (shared)
    3. OAuth user with encryption_key → per-user client from UserStore

    Raises:
        UserNotConfiguredError: When multi-user mode is active and the current
            user has not yet registered their J-Quants API key.
    """
    from fastmcp.server.dependencies import get_access_token

    from .exceptions import UserNotConfiguredError

    token = get_access_token()

    # No auth or static bearer token → use global client
    if token is None or token.client_id == "bearer":
        return _get_client()

    user_id = token.client_id
    user_db = _get_user_db()

    # encryption_key not configured → share the global client for all OAuth users
    if user_db is None:
        return _get_client()

    # Return cached per-user client if already created
    if user_id in _user_clients:
        return _user_clients[user_id]

    # Look up the user's API key from the encrypted store
    user = user_db.get_user(user_id)
    if user is None:
        raise UserNotConfiguredError(user_id)

    # Race-safe: another coroutine may have created the client while we worked
    if user_id not in _user_clients:
        user_settings = Settings(
            jquants_api_key=user.api_key,
            jquants_plan=user.plan,
        )
        _user_clients[user_id] = JQuantsClient(user_settings)

    return _user_clients[user_id]


# ------------------------------------------------------------------
# Utility tools
# ------------------------------------------------------------------


@mcp.tool()
def health_check() -> dict[str, Any]:
    """Check server health and API key configuration.

    Returns server version, API key status, and active plan.
    """
    settings = _get_settings()
    has_key = bool(settings.jquants_api_key)
    return {
        "status": "healthy",
        "service": "jquants-dat-mcp",
        "version": __version__,
        "api_key_configured": has_key,
        "plan": settings.jquants_plan,
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

    return {
        "status": "ok",
        "user_id": user_id,
        "plan": plan,
        "message": "API key registered successfully.",
    }


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

    user_id = token.client_id
    deleted = user_db.delete_user(user_id)
    _user_clients.pop(user_id, None)

    if deleted:
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
        mcp.run(transport=transport, host=host, port=port, uvicorn_config=uvicorn_config)
