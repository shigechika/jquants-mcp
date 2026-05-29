"""Derivative-related tools for jquants-mcp."""

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
from ..tool_annotations import READ_ONLY_API
from ..validators import make_validation_error_response, validate_date

logger = logging.getLogger(__name__)


def register(
    mcp: FastMCP,
    get_client: callable,
    get_cache: callable,
) -> None:
    """Register derivative tools on the MCP server."""

    @mcp.tool(annotations=READ_ONLY_API)
    async def get_derivatives_bars_daily_futures(
        date: str,
        category: str | None = None,
        contract_flag: str | None = None,
    ) -> dict[str, Any]:
        """Daily futures OHLC bars (先物日足). Premium only.

        Use for 先物, 日経先物, TOPIX先物, マザーズ先物, futures OHLC, 先物建玉.
        Returns OHLC, volume, and open interest for futures contracts.

        [Supported plans] Premium

        Args:
            date: Date (YYYYMMDD or YYYY-MM-DD) (required)
            category: Product category (e.g. Futures225 (日経225先物), FuturesTOPIX (TOPIX先物)).
                Omit for all categories.
            contract_flag: Contract month flag (0 = all, 1 = front month, 2 = back month)
        """
        err = validate_date(date)
        if err:
            return make_validation_error_response([err])

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

    @mcp.tool(annotations=READ_ONLY_API)
    async def get_derivatives_bars_daily_options(
        date: str,
        category: str | None = None,
        code: str | None = None,
        contract_flag: str | None = None,
    ) -> dict[str, Any]:
        """Daily options OHLC bars with IV (オプション日足). Premium only.

        Use for オプション, 日経オプション, TOPIXオプション, IV, implied volatility,
        オプション建玉. Returns OHLC, volume, open interest, and implied volatility.
        For Nikkei 225 options only (Standard+), use get_derivatives_bars_daily_options_225.

        [Supported plans] Premium

        Args:
            date: Date (YYYYMMDD or YYYY-MM-DD) (required)
            category: Product category (e.g. Options225 (日経225オプション), OptionsTOPIX (TOPIXオプション)).
                Omit for all categories.
            code: Issue code
            contract_flag: Contract month flag (0 = all, 1 = front month, 2 = back month)
        """
        err = validate_date(date)
        if err:
            return make_validation_error_response([err])

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

    @mcp.tool(annotations=READ_ONLY_API)
    async def get_derivatives_bars_daily_options_225(
        date: str,
    ) -> dict[str, Any]:
        """Daily Nikkei 225 options OHLC bars (日経225オプション). Standard+.

        Use for 日経225オプション, オプション日足 (simplified). Standard plan accessible.
        For full options data including TOPIX options and IV, use
        get_derivatives_bars_daily_options (Premium only) instead.

        [Supported plans] Standard / Premium

        Args:
            date: Date (YYYYMMDD or YYYY-MM-DD) (required)
        """
        err = validate_date(date)
        if err:
            return make_validation_error_response([err])

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
