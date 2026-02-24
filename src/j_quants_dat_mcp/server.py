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

mcp = FastMCP("j-quants-dat-mcp")

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
        "service": "j-quants-dat-mcp",
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


def run_server() -> None:
    """Start the MCP server."""
    logging.basicConfig(level=logging.INFO, format="%(name)s - %(levelname)s - %(message)s")
    logger.info("j-quants-dat-mcp v%s を起動します", __version__)
    mcp.run()
