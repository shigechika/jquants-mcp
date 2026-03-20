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

# グローバルな共有インスタンス（サーバー起動時に初期化）
_settings: Settings | None = None
_client: JQuantsClient | None = None
_cache: CacheStore | None = None


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
        _cache = CacheStore(db_path)
    return _cache


# ------------------------------------------------------------------
# ユーティリティツール
# ------------------------------------------------------------------


@mcp.tool()
def health_check() -> dict[str, Any]:
    """Check server health and API key configuration.

    サーバーの稼働状態と API キーの設定状況を確認する。
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

    キャッシュの統計情報（テーブル別件数、DB サイズ等）を返す。
    """
    return _get_cache().status()


@mcp.tool()
def cache_clear(table: str | None = None) -> dict[str, Any]:
    """Clear cached data.

    キャッシュをクリアする。table を指定するとそのテーブルのみクリアする。

    Args:
        table: テーブル名（省略時は全テーブル）
    """
    result = _get_cache().clear(table)
    return {"cleared": result}


# ------------------------------------------------------------------
# ツール登録（Phase 2 以降で追加）
# ------------------------------------------------------------------


def _register_tools() -> None:
    """Register all endpoint tools. Called during module import."""
    from .tools import bulk, derivatives, equities, financials, indices, markets

    equities.register(mcp, _get_client, _get_cache)
    financials.register(mcp, _get_client, _get_cache)
    indices.register(mcp, _get_client, _get_cache)
    derivatives.register(mcp, _get_client, _get_cache)
    markets.register(mcp, _get_client, _get_cache)
    bulk.register(mcp, _get_client, _get_cache)


_register_tools()


# ------------------------------------------------------------------
# サーバー起動
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

        # TLS 設定
        uvicorn_config: dict[str, Any] = {}
        if ssl_certfile and ssl_keyfile:
            uvicorn_config["ssl_certfile"] = ssl_certfile
            uvicorn_config["ssl_keyfile"] = ssl_keyfile
            scheme = "https"
        else:
            scheme = "http"

        logger.info("%s server: %s://%s:%d/mcp", transport, scheme, host, port)
        mcp.run(transport=transport, host=host, port=port, uvicorn_config=uvicorn_config)
