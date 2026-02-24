"""Market-related tools for j-quants-dat-mcp."""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP

from ..cache.store import CacheStore, TTL_7D, TTL_24H, make_cache_key
from ..client import JQuantsClient
from ..exceptions import APIError, format_api_error

logger = logging.getLogger(__name__)


def register(
    mcp: FastMCP,
    get_client: callable,
    get_cache: callable,
) -> None:
    """Register market tools on the MCP server."""

    @mcp.tool()
    async def get_markets_margin_interest(
        code: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve margin trading interest data.

        信用取引の品貸料・融資/貸株残高データを取得する。
        銘柄ごとの信用残（融資残・貸株残）、逆日歩等を取得できる。

        [対応プラン] Standard / Premium

        Args:
            code: 銘柄コード（5桁 例: 27800、4桁指定時は普通株式のみ）
            date: 日付（YYYYMMDD or YYYY-MM-DD）
            date_from: 期間指定の開始日
            date_to: 期間指定の終了日
        """
        client: JQuantsClient = get_client()
        cache: CacheStore = get_cache()

        params = {"code": code, "date": date, "from": date_from, "to": date_to}
        cache_key = make_cache_key("/markets/margin-interest", params)
        cached = cache.get_response(cache_key)
        if cached is not None:
            return cached

        try:
            data = await client.get_all_pages("/markets/margin-interest", params)
            result = {"count": len(data), "data": data}
            cache.put_response(cache_key, result, ttl_seconds=TTL_7D)
            return result
        except APIError as e:
            return format_api_error(e)

    @mcp.tool()
    async def get_markets_margin_alert(
        code: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve margin trading alert data.

        信用取引の規制情報（増担保規制）を取得する。
        規制銘柄の状態（規制開始・解除）や規制区分を確認できる。

        [対応プラン] Standard / Premium

        Args:
            code: 銘柄コード（5桁 例: 27800、4桁指定時は普通株式のみ）
            date: 日付（YYYYMMDD or YYYY-MM-DD）
            date_from: 期間指定の開始日
            date_to: 期間指定の終了日
        """
        client: JQuantsClient = get_client()
        cache: CacheStore = get_cache()

        params = {"code": code, "date": date, "from": date_from, "to": date_to}
        cache_key = make_cache_key("/markets/margin-alert", params)
        cached = cache.get_response(cache_key)
        if cached is not None:
            return cached

        try:
            data = await client.get_all_pages("/markets/margin-alert", params)
            result = {"count": len(data), "data": data}
            cache.put_response(cache_key, result, ttl_seconds=TTL_24H)
            return result
        except APIError as e:
            return format_api_error(e)

    @mcp.tool()
    async def get_markets_short_ratio(
        s33: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve short selling ratio data.

        空売り比率データを取得する。33業種区分別の空売り比率
        （実売り比率・空売り比率・信用空売り比率）を取得できる。

        [対応プラン] Standard / Premium

        Args:
            s33: 33業種コード（例: 0050 = 水産・農林業）
            date: 日付（YYYYMMDD or YYYY-MM-DD）
            date_from: 期間指定の開始日
            date_to: 期間指定の終了日
        """
        client: JQuantsClient = get_client()
        cache: CacheStore = get_cache()

        params = {"s33": s33, "date": date, "from": date_from, "to": date_to}
        cache_key = make_cache_key("/markets/short-ratio", params)
        cached = cache.get_response(cache_key)
        if cached is not None:
            return cached

        try:
            data = await client.get_all_pages("/markets/short-ratio", params)
            result = {"count": len(data), "data": data}
            cache.put_response(cache_key, result, ttl_seconds=TTL_24H)
            return result
        except APIError as e:
            return format_api_error(e)

    @mcp.tool()
    async def get_markets_short_sale_report(
        code: str | None = None,
        disc_date: str | None = None,
        disc_date_from: str | None = None,
        disc_date_to: str | None = None,
        calc_date: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve short sale position report data.

        空売り残高報告データを取得する。個別銘柄の空売り残高（報告義務発生分）を
        取得できる。開示日・算定日・残高割合などを含む。

        [対応プラン] Standard / Premium

        Args:
            code: 銘柄コード（5桁 例: 27800、4桁指定時は普通株式のみ）
            disc_date: 開示日（YYYYMMDD or YYYY-MM-DD）
            disc_date_from: 開示日の期間指定の開始日
            disc_date_to: 開示日の期間指定の終了日
            calc_date: 算定日（YYYYMMDD or YYYY-MM-DD）
        """
        client: JQuantsClient = get_client()
        cache: CacheStore = get_cache()

        params = {
            "code": code,
            "disc_date": disc_date,
            "disc_date_from": disc_date_from,
            "disc_date_to": disc_date_to,
            "calc_date": calc_date,
        }
        cache_key = make_cache_key("/markets/short-sale-report", params)
        cached = cache.get_response(cache_key)
        if cached is not None:
            return cached

        try:
            data = await client.get_all_pages("/markets/short-sale-report", params)
            result = {"count": len(data), "data": data}
            cache.put_response(cache_key, result, ttl_seconds=TTL_24H)
            return result
        except APIError as e:
            return format_api_error(e)

    @mcp.tool()
    async def get_markets_breakdown(
        code: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve market breakdown data (sell/buy by investor type per issue).

        売買内訳データを取得する。個別銘柄の投資部門別（自己・委託・海外等）の
        売り買い内訳を日次で取得できる。

        [対応プラン] Premium

        Args:
            code: 銘柄コード（5桁 例: 27800、4桁指定時は普通株式のみ）
            date: 日付（YYYYMMDD or YYYY-MM-DD）
            date_from: 期間指定の開始日
            date_to: 期間指定の終了日
        """
        client: JQuantsClient = get_client()
        cache: CacheStore = get_cache()

        params = {"code": code, "date": date, "from": date_from, "to": date_to}
        cache_key = make_cache_key("/markets/breakdown", params)
        cached = cache.get_response(cache_key)
        if cached is not None:
            return cached

        try:
            data = await client.get_all_pages("/markets/breakdown", params)
            result = {"count": len(data), "data": data}
            cache.put_response(cache_key, result, ttl_seconds=TTL_24H)
            return result
        except APIError as e:
            return format_api_error(e)

    @mcp.tool()
    async def get_markets_calendar(
        hol_div: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve market calendar (trading days and holidays).

        取引カレンダーを取得する。営業日・祝日・半休日の区分を含む。
        ページネーションなしで全データを一括返却する。

        [対応プラン] Free / Light / Standard / Premium

        Args:
            hol_div: 休日区分フィルタ（例: 0 = 営業日, 1 = 祝日, 2 = 特別休日）
            date_from: 期間指定の開始日（YYYYMMDD or YYYY-MM-DD）
            date_to: 期間指定の終了日（YYYYMMDD or YYYY-MM-DD）
        """
        client: JQuantsClient = get_client()
        cache: CacheStore = get_cache()

        params = {"hol_div": hol_div, "from": date_from, "to": date_to}
        cache_key = make_cache_key("/markets/calendar", params)
        cached = cache.get_response(cache_key)
        if cached is not None:
            return cached

        try:
            # ページネーションなし → client.get() を直接使用
            response = await client.get("/markets/calendar", params)
            data = response.get("data", [])
            result = {"count": len(data), "data": data}
            cache.put_response(cache_key, result, ttl_seconds=TTL_7D)
            return result
        except APIError as e:
            return format_api_error(e)
