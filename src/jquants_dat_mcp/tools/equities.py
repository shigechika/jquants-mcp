"""Equity-related tools for jquants-dat-mcp."""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP

from ..cache.store import ENDPOINT_TTL, CacheStore, TTL_24H, TTL_90D, make_cache_key
from ..client import JQuantsClient
from ..exceptions import APIError, InvalidAPIKeyError, UserNotConfiguredError, format_api_error
from ..validators import (
    collect_errors,
    make_validation_error_response,
    validate_code,
    validate_date,
    validate_section,
)

logger = logging.getLogger(__name__)


def register(
    mcp: FastMCP,
    get_client: callable,
    get_cache: callable,
) -> None:
    """Register equity tools on the MCP server."""

    @mcp.tool()
    async def get_equities_master(
        code: str | None = None,
        date: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve listed issue information.

        Returns information on listed stocks including company name, industry, and market segment.
        When parameters are omitted, returns all listed stocks for today.

        [Supported plans] Free / Light / Standard / Premium

        Args:
            code: Stock code (5 digits, e.g. 27800; 4-digit codes match ordinary shares only)
            date: Base date (YYYYMMDD or YYYY-MM-DD)
        """
        errors = collect_errors(validate_code(code), validate_date(date))
        if errors:
            return make_validation_error_response(errors)

        client: JQuantsClient = await get_client()
        cache: CacheStore = get_cache()

        params = {"code": code, "date": date}
        cache_key = make_cache_key("/equities/master", params)
        cached = cache.get_response(cache_key)
        if cached is not None:
            return cached

        try:
            data = await client.get_all_pages("/equities/master", params)
            result = {"count": len(data), "data": data}
            cache.put_response(cache_key, result, ttl_seconds=TTL_24H)
            return result
        except (APIError, InvalidAPIKeyError, UserNotConfiguredError) as e:
            return format_api_error(e)

    @mcp.tool()
    async def get_equities_bars_daily(
        code: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve daily stock prices (OHLC).

        Returns daily OHLC data for stocks. Either 'code' or 'date' must be specified.
        Includes adjusted prices (AdjO/AdjC, etc.) and morning/afternoon session breakdown.

        [Supported plans] Free / Light / Standard / Premium
        Note: Free plan data is delayed by 12 weeks.

        Args:
            code: Stock code (5 digits, e.g. 27800; 4-digit codes match ordinary shares only)
            date: Date (YYYYMMDD or YYYY-MM-DD)
            date_from: Start date for range query
            date_to: End date for range query
        """
        if code is None and date is None and date_from is None and date_to is None:
            return make_validation_error_response(
                ["Either 'code' or 'date' (or date_from/date_to) must be specified."]
            )
        errors = collect_errors(
            validate_code(code),
            validate_date(date),
            validate_date(date_from, "date_from"),
            validate_date(date_to, "date_to"),
        )
        if errors:
            return make_validation_error_response(errors)

        client: JQuantsClient = await get_client()
        cache: CacheStore = get_cache()

        # code 指定 + 期間指定の場合は Tier 1 キャッシュで増分取得
        if code and (date_from or date_to or date):
            return await _get_bars_daily_with_cache(client, cache, code, date, date_from, date_to)

        # date のみ指定（全銘柄）の場合は Tier 2 キャッシュ
        params = {"code": code, "date": date, "from": date_from, "to": date_to}
        cache_key = make_cache_key("/equities/bars/daily", params)
        cached = cache.get_response(cache_key)
        if cached is not None:
            return cached

        try:
            data = await client.get_all_pages("/equities/bars/daily", params)
            result = {"count": len(data), "data": data}
            cache.put_response(cache_key, result, ttl_seconds=TTL_24H)
            return result
        except (APIError, InvalidAPIKeyError, UserNotConfiguredError) as e:
            return format_api_error(e)

    @mcp.tool()
    async def get_equities_bars_minute(
        code: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve minute-level stock prices (OHLC).

        Returns 1-minute OHLC, volume, and trading value for stocks.
        Data is available for up to 2 years in the past.

        [Supported plans] Light / Standard / Premium (requires minute/tick data add-on)

        Args:
            code: Stock code (5 digits, e.g. 27800; 4-digit codes match ordinary shares only)
            date: Date (YYYYMMDD or YYYY-MM-DD)
            date_from: Start date for range query
            date_to: End date for range query
        """
        errors = collect_errors(
            validate_code(code),
            validate_date(date),
            validate_date(date_from, "date_from"),
            validate_date(date_to, "date_to"),
        )
        if errors:
            return make_validation_error_response(errors)

        client: JQuantsClient = await get_client()
        cache: CacheStore = get_cache()

        params = {"code": code, "date": date, "from": date_from, "to": date_to}
        cache_key = make_cache_key("/equities/bars/minute", params)
        cached = cache.get_response(cache_key)
        if cached is not None:
            return cached

        try:
            data = await client.get_all_pages("/equities/bars/minute", params)
            result = {"count": len(data), "data": data}
            cache.put_response(cache_key, result, ttl_seconds=TTL_24H)
            return result
        except (APIError, InvalidAPIKeyError, UserNotConfiguredError) as e:
            return format_api_error(e)

    @mcp.tool()
    async def get_equities_bars_daily_am(
        code: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve morning session stock prices (OHLC).

        Returns today's morning session OHLC and volume.
        Updated around 12:00 JST and available until around 6:00 JST the next day.
        Historical morning session data (MO/MH/ML/MC columns) is available via get_equities_bars_daily.

        [Supported plans] Premium

        Args:
            code: Stock code (5 digits, e.g. 27800; 4-digit codes match ordinary shares only).
                  Omit to retrieve all stocks.
        """
        errors = collect_errors(validate_code(code))
        if errors:
            return make_validation_error_response(errors)

        client: JQuantsClient = await get_client()

        # リアルタイムデータのためキャッシュしない
        try:
            data = await client.get_all_pages("/equities/bars/daily/am", {"code": code})
            return {"count": len(data), "data": data}
        except (APIError, InvalidAPIKeyError, UserNotConfiguredError) as e:
            return format_api_error(e)

    @mcp.tool()
    async def get_equities_investor_types(
        section: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve trading data by type of investors.

        Returns weekly trading value broken down by investor type
        (proprietary, brokered, foreign investors, individuals, trust banks, etc.).
        Updated weekly, typically on Thursdays.

        [Supported plans] Light / Standard / Premium

        Args:
            section: Market section (e.g. TSEPrime, TSEStandard, TSEGrowth)
            date_from: Start date for range query (YYYYMMDD or YYYY-MM-DD)
            date_to: End date for range query (YYYYMMDD or YYYY-MM-DD)
        """
        errors = collect_errors(
            validate_section(section),
            validate_date(date_from, "date_from"),
            validate_date(date_to, "date_to"),
        )
        if errors:
            return make_validation_error_response(errors)

        client: JQuantsClient = await get_client()
        cache: CacheStore = get_cache()

        params = {"section": section, "from": date_from, "to": date_to}
        cache_key = make_cache_key("/equities/investor-types", params)
        cached = cache.get_response(cache_key)
        if cached is not None:
            return cached

        try:
            data = await client.get_all_pages("/equities/investor-types", params)
            result = {"count": len(data), "data": data}
            ttl = ENDPOINT_TTL.get("/equities/investor-types", TTL_24H)
            cache.put_response(cache_key, result, ttl_seconds=ttl)
            return result
        except (APIError, InvalidAPIKeyError, UserNotConfiguredError) as e:
            return format_api_error(e)

    @mcp.tool()
    async def get_equities_earnings_calendar(
        date: str | None = None,
        code: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve earnings announcement schedule.

        Returns up to ~3 months of accumulated earnings announcement schedule.
        Specify date for announcements on that day, or code to search upcoming earnings dates.
        Covers March and September fiscal year companies (REITs excluded).

        [Supported plans] Free / Light / Standard / Premium

        Args:
            date: Announcement date (YYYYMMDD or YYYY-MM-DD). Returns latest data when omitted.
            code: Stock code (5 digits, e.g. 72030; 4-digit codes are padded with trailing 0).
                  When specified, searches accumulated data for the matching stock's earnings dates.
        """
        errors = collect_errors(validate_code(code), validate_date(date))
        if errors:
            return make_validation_error_response(errors)

        client: JQuantsClient = await get_client()
        cache: CacheStore = get_cache()

        # 銘柄コード検索: 蓄積データから該当銘柄を抽出
        if code is not None:
            if len(code) == 4:
                code = code + "0"
            return _search_earnings_by_code(cache, code)

        # 日付指定: 蓄積データから取得
        if date is not None:
            date_key = date.replace("-", "")
            cache_key = make_cache_key("/equities/earnings-calendar", {"date": date_key})
            cached = cache.get_response(cache_key)
            if cached is not None:
                if isinstance(cached, list):
                    cached = {"count": len(cached), "data": cached}
                return cached
            return {"count": 0, "data": [], "message": f"日付 {date} のデータなし"}

        # パラメータなし: 最新データ
        cache_key = make_cache_key("/equities/earnings-calendar")
        cached = cache.get_response(cache_key)
        if cached is not None:
            # daily_fetch.py が生リストで保存した場合を吸収
            if isinstance(cached, list):
                cached = {"count": len(cached), "data": cached}
            return cached

        try:
            data = await client.get_all_pages("/equities/earnings-calendar")
            result = {"count": len(data), "data": data}
            cache.put_response(cache_key, result, ttl_seconds=TTL_90D)
            return result
        except (APIError, InvalidAPIKeyError, UserNotConfiguredError) as e:
            return format_api_error(e)

    def _search_earnings_by_code(cache: CacheStore, code: str) -> dict[str, Any]:
        """Search accumulated earnings calendar data for a specific stock code."""
        import json

        conn = cache._ensure_connection()
        # cache_key format: "/equities/earnings-calendar|date=YYYYMMDD|plan=<plan>"
        plan = cache.default_plan
        rows = conn.execute(
            "SELECT data FROM response_cache WHERE cache_key LIKE ? AND cache_key LIKE ?",
            (
                "/equities/earnings-calendar|date=%",
                f"%|plan={plan}",
            ),
        ).fetchall()

        matches = []
        seen_dates = set()
        for row in rows:
            records = json.loads(row["data"])
            if isinstance(records, dict):
                records = records.get("data", [])
            for rec in records:
                rec_code = str(rec.get("Code", ""))
                if rec_code == code:
                    date = rec.get("Date", "")
                    if date not in seen_dates:
                        seen_dates.add(date)
                        matches.append(rec)

        matches.sort(key=lambda r: r.get("Date", ""), reverse=True)
        return {"count": len(matches), "data": matches}


# ------------------------------------------------------------------
# Tier 1 キャッシュ: 株価四本値の増分取得
# ------------------------------------------------------------------


async def _get_bars_daily_with_cache(
    client: JQuantsClient,
    cache: CacheStore,
    code: str,
    date: str | None,
    date_from: str | None,
    date_to: str | None,
) -> dict[str, Any]:
    """株価四本値を Tier 1 キャッシュ付きで取得する。"""
    # 4桁コードを5桁に正規化（普通株式: 末尾0）
    cache_code = code + "0" if len(code) == 4 else code

    try:
        # まず最新1件をAPIから取得して分割チェック
        probe_params: dict[str, Any] = {"code": code}
        if date:
            probe_params["date"] = date
        elif date_to:
            probe_params["date"] = date_to

        # キャッシュから既存データを取得（5桁コードで検索）
        effective_date = date or date_from
        cached_data = cache.get_rows(
            "equities_bars_daily",
            key_filter={"code": cache_code},
            date_from=effective_date or date_from,
            date_to=date_to,
        )

        # API にリクエスト（元のコードをそのまま渡す）
        params: dict[str, Any] = {"code": code}
        if date:
            params["date"] = date
        if date_from:
            params["from"] = date_from
        if date_to:
            params["to"] = date_to

        # キャッシュ済み日付の確認
        cached_dates = cache.get_cached_dates(
            "equities_bars_daily",
            key_filter={"code": cache_code},
            date_from=date_from or date,
            date_to=date_to,
        )

        if cached_dates and not date:
            # 増分取得: キャッシュにない期間のみ API から取得
            latest_cached = max(cached_dates)
            if date_to and latest_cached >= date_to:
                # 全期間キャッシュ済み
                logger.info("全データキャッシュ済み: code=%s (%d件)", code, len(cached_data))
                return {"count": len(cached_data), "data": cached_data, "source": "cache"}

            # 最新日以降を取得
            params["from"] = latest_cached
            if date_to:
                params["to"] = date_to

        try:
            api_data = await client.get_all_pages("/equities/bars/daily", params)
        except APIError:
            # API 失敗でもキャッシュデータがあればそれを返す
            if cached_data:
                logger.info(
                    "API失敗、キャッシュデータを返却: code=%s (%d件)", code, len(cached_data)
                )
                return {"count": len(cached_data), "data": cached_data, "source": "cache"}
            raise

        if api_data:
            # 株式分割チェック
            latest_row = api_data[-1]
            adj_factor = latest_row.get("AdjFactor")
            if not cache.check_adj_factor(cache_code, adj_factor):
                # 分割検知 → キャッシュ無効化して全件再取得
                cache.invalidate_rows("equities_bars_daily", {"code": cache_code})
                logger.info("株式分割検知、キャッシュ再取得: code=%s", code)
                params_full = {"code": code}
                if date_from:
                    params_full["from"] = date_from
                if date_to:
                    params_full["to"] = date_to
                if date:
                    params_full["date"] = date
                api_data = await client.get_all_pages("/equities/bars/daily", params_full)
                cached_data = []

            # キャッシュに保存
            cache.put_rows(
                "equities_bars_daily",
                api_data,
                key_columns=["Code", "Date"],
                adj_factor_key="AdjFactor",
            )

        # キャッシュデータと API データをマージ（重複排除）
        seen_keys: set[str] = set()
        merged: list[dict[str, Any]] = []
        for row in api_data:
            key = f"{row.get('Code')}_{row.get('Date')}"
            if key not in seen_keys:
                seen_keys.add(key)
                merged.append(row)
        for row in cached_data:
            key = f"{row.get('Code')}_{row.get('Date')}"
            if key not in seen_keys:
                seen_keys.add(key)
                merged.append(row)

        # 日付でソート
        merged.sort(key=lambda r: r.get("Date", ""))

        source = "cache+api" if cached_data and api_data else ("cache" if cached_data else "api")
        return {"count": len(merged), "data": merged, "source": source}

    except (APIError, InvalidAPIKeyError, UserNotConfiguredError) as e:
        return format_api_error(e)
