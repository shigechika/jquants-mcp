"""Derivative-related tools for j-quants-dat-mcp."""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP

from ..cache.store import CacheStore, TTL_24H, make_cache_key
from ..client import JQuantsClient
from ..exceptions import APIError, format_api_error

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

        先物の日足四本値を取得する。日経225先物・TOPIX先物・マザーズ先物など
        各種先物の始値・高値・安値・終値・出来高・建玉を日次で取得できる。

        [対応プラン] Premium

        Args:
            date: 日付（YYYYMMDD or YYYY-MM-DD）（必須）
            category: 商品区分（例: Futures225, FuturesTOPIX）
            contract_flag: 限月フラグ（例: 0 = 全限月, 1 = 期近, 2 = 期先）
        """
        client: JQuantsClient = get_client()
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
        except APIError as e:
            return format_api_error(e)

    @mcp.tool()
    async def get_derivatives_bars_daily_options(
        date: str,
        category: str | None = None,
        code: str | None = None,
        contract_flag: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve daily options bars (OHLC).

        オプションの日足四本値を取得する。日経225オプション・TOPIXオプションなど
        各種オプションの始値・高値・安値・終値・出来高・建玉・IV を日次で取得できる。

        [対応プラン] Premium

        Args:
            date: 日付（YYYYMMDD or YYYY-MM-DD）（必須）
            category: 商品区分（例: Options225, OptionsTOPIX）
            code: 銘柄コード
            contract_flag: 限月フラグ（例: 0 = 全限月, 1 = 期近, 2 = 期先）
        """
        client: JQuantsClient = get_client()
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
        except APIError as e:
            return format_api_error(e)

    @mcp.tool()
    async def get_derivatives_bars_daily_options_225(
        date: str,
    ) -> dict[str, Any]:
        """Retrieve daily Nikkei 225 options bars (OHLC).

        日経225オプションの日足四本値を取得する。Standard プラン以上で利用可能な
        簡易版エンドポイント。始値・高値・安値・終値・出来高・建玉を取得できる。

        [対応プラン] Standard / Premium

        Args:
            date: 日付（YYYYMMDD or YYYY-MM-DD）（必須）
        """
        client: JQuantsClient = get_client()
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
        except APIError as e:
            return format_api_error(e)
