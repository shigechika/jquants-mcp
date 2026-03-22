"""Bulk download tools for jquants-dat-mcp."""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP

from ..cache.store import CacheStore, TTL_6H, make_cache_key
from ..client import JQuantsClient
from ..exceptions import APIError, InvalidAPIKeyError, UserNotConfiguredError, format_api_error

logger = logging.getLogger(__name__)

# /bulk/list の endpoint パラメータに指定可能な値
VALID_BULK_ENDPOINTS = [
    "/equities/master",
    "/equities/bars/daily",
    "/equities/bars/minute",
    "/equities/investor-types",
    "/fins/summary",
    "/fins/details",
    "/fins/dividend",
    "/indices/bars/daily",
    "/indices/bars/daily/topix",
    "/derivatives/bars/daily/futures",
    "/derivatives/bars/daily/options",
    "/derivatives/bars/daily/options/225",
    "/markets/margin-interest",
    "/markets/margin-alert",
    "/markets/short-ratio",
    "/markets/short-sale-report",
    "/markets/breakdown",
    "/equities/trades",
]


def register(
    mcp: FastMCP,
    get_client: callable,
    get_cache: callable,
) -> None:
    """Register bulk download tools on the MCP server."""

    @mcp.tool()
    async def get_bulk_list(
        endpoint: str,
    ) -> dict[str, Any]:
        """Retrieve list of downloadable CSV files.

        CSV 形式でダウンロード可能なファイルの一覧を取得する。
        endpoint パラメータでデータセットを指定し、ダウンロード用キー（Key）・
        最終更新日時・ファイルサイズの一覧を返す。

        取得した Key は get_bulk_download_url で署名付き URL に変換してダウンロードできる。

        [対応プラン] Light / Standard / Premium

        Args:
            endpoint: データセットのエンドポイント名（例: /equities/bars/daily）。
                指定可能な値:
                /equities/master, /equities/bars/daily, /equities/bars/minute,
                /equities/investor-types, /fins/summary, /fins/details,
                /fins/dividend, /indices/bars/daily, /indices/bars/daily/topix,
                /derivatives/bars/daily/futures, /derivatives/bars/daily/options,
                /derivatives/bars/daily/options/225, /markets/margin-interest,
                /markets/margin-alert, /markets/short-ratio,
                /markets/short-sale-report, /markets/breakdown, /equities/trades
        """
        if endpoint not in VALID_BULK_ENDPOINTS:
            return {
                "error": True,
                "message": f"無効な endpoint: {endpoint}",
                "hint": f"指定可能な値: {', '.join(VALID_BULK_ENDPOINTS)}",
            }

        client: JQuantsClient = await get_client()
        cache: CacheStore = get_cache()

        params = {"endpoint": endpoint}
        cache_key = make_cache_key("/bulk/list", params)
        cached = cache.get_response(cache_key)
        if cached is not None:
            return cached

        try:
            data = await client.get_all_pages("/bulk/list", params)
            result = {"count": len(data), "data": data}
            cache.put_response(cache_key, result, ttl_seconds=TTL_6H)
            return result
        except (APIError, InvalidAPIKeyError, UserNotConfiguredError) as e:
            return format_api_error(e)

    @mcp.tool()
    async def get_bulk_download_url(
        key: str,
    ) -> dict[str, Any]:
        """Retrieve a signed URL for downloading a CSV file.

        get_bulk_list で取得した Key を指定し、CSV ファイルダウンロード用の
        署名付き URL を取得する。URL の有効期限は約5分。

        [対応プラン] Light / Standard / Premium

        Args:
            key: ファイルのキー（get_bulk_list で取得した Key 値）
        """
        client: JQuantsClient = await get_client()

        # 署名付き URL は一時的なため、キャッシュしない
        try:
            response = await client.get("/bulk/get", {"key": key})
            url = response.get("url", "")
            return {
                "url": url,
                "hint": "URL の有効期限は約5分です。期限内にダウンロードしてください。",
            }
        except (APIError, InvalidAPIKeyError, UserNotConfiguredError) as e:
            return format_api_error(e)
