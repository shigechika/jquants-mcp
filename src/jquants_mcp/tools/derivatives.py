"""Derivative-related tools for jquants-dat-mcp."""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP

from ..cache.store import CacheStore, TTL_24H, make_cache_key
from ..client import JQuantsClient
from ..exceptions import (
    APIError,
    DecryptionError,
    InvalidAPIKeyError,
    UserNotAllowedError,
    UserNotConfiguredError,
    format_api_error,
)

logger = logging.getLogger(__name__)


def register(
    mcp: FastMCP,
    get_client: callable,
    get_cache: callable,
) -> None:
    """Register derivative tools on the MCP server."""

    @mcp.tool()
    async def get_derivatives_bars_daily_futures(
        date: str,
        category: str | None = None,
        contract_flag: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve daily futures bars (OHLC).

        Returns daily OHLC, volume, and open interest for futures contracts
        including Nikkei 225 futures, TOPIX futures, and Mothers futures.

        [Supported plans] Premium

        Args:
            date: Date (YYYYMMDD or YYYY-MM-DD) (required)
            category: Product category (e.g. Futures225, FuturesTOPIX)
            contract_flag: Contract month flag (e.g. 0 = all, 1 = front month, 2 = back month)
        """
        client: JQuantsClient = await get_client()
        cache: CacheStore = get_cache()

        params = {"date": date, "category": category, "contract_flag": contract_flag}
        cache_key = make_cache_key("/derivatives/bars/daily/futures", params)
        cached = cache.get_response(cache_key)
        if cached is not None:
            return cached

        try:
            data = await client.get_all_pages("/derivatives/bars/daily/futures", params)
            result = {"count": len(data), "data": data}
            cache.put_response(cache_key, result, ttl_seconds=TTL_24H)
            return result
        except (
            APIError,
            InvalidAPIKeyError,
            UserNotConfiguredError,
            DecryptionError,
            UserNotAllowedError,
        ) as e:
            return format_api_error(e)

    @mcp.tool()
    async def get_derivatives_bars_daily_options(
        date: str,
        category: str | None = None,
        code: str | None = None,
        contract_flag: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve daily options bars (OHLC).

        Returns daily OHLC, volume, open interest, and implied volatility for options
        including Nikkei 225 options and TOPIX options.

        [Supported plans] Premium

        Args:
            date: Date (YYYYMMDD or YYYY-MM-DD) (required)
            category: Product category (e.g. Options225, OptionsTOPIX)
            code: Issue code
            contract_flag: Contract month flag (e.g. 0 = all, 1 = front month, 2 = back month)
        """
        client: JQuantsClient = await get_client()
        cache: CacheStore = get_cache()

        params = {
            "date": date,
            "category": category,
            "code": code,
            "contract_flag": contract_flag,
        }
        cache_key = make_cache_key("/derivatives/bars/daily/options", params)
        cached = cache.get_response(cache_key)
        if cached is not None:
            return cached

        try:
            data = await client.get_all_pages("/derivatives/bars/daily/options", params)
            result = {"count": len(data), "data": data}
            cache.put_response(cache_key, result, ttl_seconds=TTL_24H)
            return result
        except (
            APIError,
            InvalidAPIKeyError,
            UserNotConfiguredError,
            DecryptionError,
            UserNotAllowedError,
        ) as e:
            return format_api_error(e)

    @mcp.tool()
    async def get_derivatives_bars_daily_options_225(
        date: str,
    ) -> dict[str, Any]:
        """Retrieve daily Nikkei 225 options bars (OHLC).

        Returns daily OHLC, volume, and open interest for Nikkei 225 options.
        This is a simplified endpoint available from the Standard plan.

        [Supported plans] Standard / Premium

        Args:
            date: Date (YYYYMMDD or YYYY-MM-DD) (required)
        """
        client: JQuantsClient = await get_client()
        cache: CacheStore = get_cache()

        params = {"date": date}
        cache_key = make_cache_key("/derivatives/bars/daily/options/225", params)
        cached = cache.get_response(cache_key)
        if cached is not None:
            return cached

        try:
            data = await client.get_all_pages("/derivatives/bars/daily/options/225", params)
            result = {"count": len(data), "data": data}
            cache.put_response(cache_key, result, ttl_seconds=TTL_24H)
            return result
        except (
            APIError,
            InvalidAPIKeyError,
            UserNotConfiguredError,
            DecryptionError,
            UserNotAllowedError,
        ) as e:
            return format_api_error(e)
