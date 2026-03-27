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

        Returns a list of downloadable CSV files for the specified dataset endpoint,
        including the download key (Key), last modified time, and file size.

        Use the returned Key with get_bulk_download_url to obtain a signed download URL.

        [Supported plans] Light / Standard / Premium

        Args:
            endpoint: Dataset endpoint name (e.g. /equities/bars/daily).
                Accepted values:
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
                "message": f"Invalid endpoint: {endpoint}",
                "hint": f"Accepted values: {', '.join(VALID_BULK_ENDPOINTS)}",
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

        Specify a Key obtained from get_bulk_list to get a signed URL for CSV download.
        The URL expires in approximately 5 minutes.

        [Supported plans] Light / Standard / Premium

        Args:
            key: File key obtained from get_bulk_list
        """
        client: JQuantsClient = await get_client()

        # 署名付き URL は一時的なため、キャッシュしない
        try:
            response = await client.get("/bulk/get", {"key": key})
            url = response.get("url", "")
            return {
                "url": url,
                "hint": "The URL expires in approximately 5 minutes. Download within the expiry time.",
            }
        except (APIError, InvalidAPIKeyError, UserNotConfiguredError) as e:
            return format_api_error(e)
