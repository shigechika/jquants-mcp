"""Equity-related tools for jquants-mcp."""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from fastmcp import FastMCP

from ..cache.store import ENDPOINT_TTL, CacheStore, TTL_24H, TTL_90D, make_cache_key
from ..client import JQuantsClient
from ..exceptions import (
    APIError,
    DecryptionError,
    InvalidAPIKeyError,
    UserNotAllowedError,
    UserNotConfiguredError,
    format_api_error,
)
from ..tool_annotations import READ_ONLY_API, READ_ONLY_CACHE
from ..validators import (
    collect_errors,
    display_code,
    make_validation_error_response,
    validate_code,
    validate_date,
    validate_section,
)

logger = logging.getLogger(__name__)


def _normalize_earnings_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize Date field to YYYY-MM-DD in earnings calendar records."""
    for rec in records:
        raw = rec.get("Date")
        if raw:
            rec["Date"] = str(raw)[:10]
    return records


def register(
    mcp: FastMCP,
    get_client: callable,
    get_cache: callable,
) -> None:
    """Register equity tools on the MCP server."""

    @mcp.tool(annotations=READ_ONLY_API)
    async def get_equities_master(
        code: str | None = None,
        date: str | None = None,
    ) -> dict[str, Any]:
        """Listed stock master: company name, industry code, market segment (上場銘柄マスタ). Free.

        Use for 銘柄名, 会社名, 業種, 市場区分, 上場銘柄一覧, S17/S33 sector code, ticker lookup.
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
        except (
            APIError,
            InvalidAPIKeyError,
            UserNotConfiguredError,
            DecryptionError,
            UserNotAllowedError,
        ) as e:
            return format_api_error(e)

    @mcp.tool(annotations=READ_ONLY_API)
    async def get_equities_bars_daily(
        code: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Per-stock or per-day OHLC bars (日足株価). All plans.

        Use for 株価・日足・OHLC・終値・出来高・調整株価 (AdjC/AdjO etc.) queries.
        For multi-stock bulk downloads use get_bulk_list (date-only queries are very slow).
        code only → full history; code+range → period; date only → all stocks on that date.

        [Supported plans] Free / Light / Standard / Premium (API fallback on cache miss)
        Retention: Free=2y (12w delay), Light=5y, Standard=10y, Premium=all.

        Args:
            code: Stock code (5 digits, e.g. 27800; 4-digit codes match ordinary shares only).
            date: Single date (YYYYMMDD or YYYY-MM-DD).
            date_from: Range start inclusive (YYYYMMDD or YYYY-MM-DD).
            date_to: Range end inclusive (YYYYMMDD or YYYY-MM-DD).
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
        except (
            APIError,
            InvalidAPIKeyError,
            UserNotConfiguredError,
            DecryptionError,
            UserNotAllowedError,
        ) as e:
            return format_api_error(e)

    @mcp.tool(annotations=READ_ONLY_API)
    async def get_equities_bars_minute(
        code: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Per-stock 1-minute OHLC bars (分足株価). Light+ with tick add-on.

        Use for 分足, 1分足, 分足データ, minute-level price, intraday OHLC.
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
        except (
            APIError,
            InvalidAPIKeyError,
            UserNotConfiguredError,
            DecryptionError,
            UserNotAllowedError,
        ) as e:
            return format_api_error(e)

    @mcp.tool(annotations=READ_ONLY_API)
    async def get_equities_bars_daily_am(
        code: str | None = None,
    ) -> dict[str, Any]:
        """Today's morning session OHLC bars (前場株価). Premium only.

        Use for 前場, 前場終値, 午前の株価, morning session OHLC, 前場引け.
        Updated around 12:00 JST; expires around 6:00 JST next day.
        For historical morning session data (MO/MH/ML/MC columns), use get_equities_bars_daily instead.

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
        except (
            APIError,
            InvalidAPIKeyError,
            UserNotConfiguredError,
            DecryptionError,
            UserNotAllowedError,
        ) as e:
            return format_api_error(e)

    @mcp.tool(annotations=READ_ONLY_API)
    async def get_equities_investor_types(
        section: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Weekly trading value by investor type (投資家別売買動向). Light+.

        Use for 投資家別売買, 外国人買い, 個人投資家動向, 信託銀行売買, investor flow.
        Updated weekly (typically Thursdays). Covers proprietary, brokered,
        foreign investors, individuals, trust banks, etc.

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
        except (
            APIError,
            InvalidAPIKeyError,
            UserNotConfiguredError,
            DecryptionError,
            UserNotAllowedError,
        ) as e:
            return format_api_error(e)

    @mcp.tool(annotations=READ_ONLY_API)
    async def get_equities_earnings_calendar(
        date: str | None = None,
        code: str | None = None,
    ) -> dict[str, Any]:
        """Find upcoming/past earnings announcement dates (決算発表日). Free / all plans.

        Use for 決算発表, 決算日, 決算スケジュール, earnings calendar, 今週決算がある銘柄,
        〇〇の次の決算はいつ, days to earnings, 決算前銘柄スクリーニング.
        Pair with get_markets_short_sale_report for 決算またぎ空売り残 / 踏み上げリスク screening.
        Covers March/September fiscal year companies (REITs excluded); ~3 months accumulated.

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

        # Stock code query: Tier 1 O(log n) lookup, fallback to Tier 2 LIKE scan
        if code is not None:
            if len(code) == 4:
                code = code + "0"
            return _search_earnings_by_code(cache, code)

        # Date filter: Tier 1 first, fallback to Tier 2 response cache
        if date is not None:
            norm_date = date if "-" in date else f"{date[:4]}-{date[4:6]}-{date[6:]}"
            t1_rows = _get_earnings_by_date_tier1(cache, norm_date)
            if t1_rows:
                return {"count": len(t1_rows), "data": t1_rows}
            date_key = date.replace("-", "")
            cache_key = make_cache_key("/equities/earnings-calendar", {"date": date_key})
            cached = cache.get_response(cache_key)
            if cached is not None:
                recs = cached if isinstance(cached, list) else cached.get("data", [])
                recs = _normalize_earnings_records(recs)
                return {"count": len(recs), "data": recs}
            return {"count": 0, "data": [], "message": f"No data for date {date}."}

        # No filter: use Tier 2 response cache (latest accumulated data)
        cache_key = make_cache_key("/equities/earnings-calendar")
        cached = cache.get_response(cache_key)
        if cached is not None:
            recs = cached if isinstance(cached, list) else cached.get("data", [])
            recs = _normalize_earnings_records(recs)
            return {"count": len(recs), "data": recs}

        try:
            data = await client.get_all_pages("/equities/earnings-calendar")
            data = _normalize_earnings_records(data)
            result = {"count": len(data), "data": data}
            cache.put_response(cache_key, result, ttl_seconds=TTL_90D)
            return result
        except (
            APIError,
            InvalidAPIKeyError,
            UserNotConfiguredError,
            DecryptionError,
            UserNotAllowedError,
        ) as e:
            return format_api_error(e)

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def search_equities(name: str) -> dict[str, Any]:
        """Search for listed stocks by company name (reverse lookup: 会社名 → コード).

        Use when the user knows a company name but not the stock code — e.g. "住友商事
        のコードは？" or "トヨタ関連銘柄を調べて". Performs a case-insensitive partial
        match against both the Japanese name (CoName) and English name (CoNameEn) fields
        in the equities master cache.

        Reads entirely from the local ``equities_master`` Tier 1 cache (no API call).
        Returns an empty list when the cache has never been populated.

        [Supported plans] Free / Light / Standard / Premium
        [Source] equities_master Tier 1 cache (no API call)

        Args:
            name: Partial or full company name to search for (e.g. "住友商事", "トヨタ",
                "Sumitomo"). Case-insensitive; matches anywhere in the name.
        """
        query = name.strip() if name else ""
        if not query:
            return make_validation_error_response(["'name' must be a non-empty string."])

        cache: CacheStore = get_cache()

        rows = cache.get_rows("equities_master", key_filter={})

        # Deduplicate: keep the most recent row per code.
        latest_by_code: dict[str, dict[str, Any]] = {}
        for row in rows:
            code = str(row.get("Code") or "")
            if not code:
                continue
            prev = latest_by_code.get(code)
            if prev is None or (row.get("Date") or "") > (prev.get("Date") or ""):
                latest_by_code[code] = row

        query_lower = query.lower()
        matches: list[dict[str, Any]] = []
        for code, row in latest_by_code.items():
            coname = str(row.get("CoName") or "")
            coname_en = str(row.get("CoNameEn") or "")
            if query_lower not in coname.lower() and query_lower not in coname_en.lower():
                continue
            entry: dict[str, Any] = {
                "code": display_code(code),
                "name": coname or coname_en or None,
            }
            if coname_en:
                entry["name_en"] = coname_en
            market = row.get("MarketCodeName") or row.get("MarketCode")
            if market:
                entry["market"] = market
            sector = row.get("Sector33CodeName")
            if sector:
                entry["sector"] = sector
            matches.append(entry)

        matches.sort(key=lambda r: r["code"])
        return {"count": len(matches), "query": query, "data": matches}

    def _search_earnings_by_code(cache: CacheStore, code: str) -> dict[str, Any]:
        """Search accumulated earnings calendar data for a specific stock code.

        Queries the Tier 1 equities_earnings_calendar table first (O(log n) index
        lookup). Falls back to the legacy Tier 2 response_cache LIKE scan when Tier 1
        is empty (e.g. before the first daily_fetch run after upgrading).
        """
        conn = cache._ensure_connection()
        if conn is None:
            return {"count": 0, "data": []}

        try:
            rows = conn.execute(
                "SELECT data FROM equities_earnings_calendar WHERE code = ? ORDER BY date",
                (code,),
            ).fetchall()
            if rows:
                matches = sorted(
                    _normalize_earnings_records([json.loads(r["data"]) for r in rows]),
                    key=lambda r: r.get("Date", ""),
                    reverse=True,
                )
                return {"count": len(matches), "data": matches}
        except sqlite3.OperationalError:
            pass

        # Tier 2 fallback: scan date-keyed response_cache entries
        try:
            t2_rows = conn.execute(
                "SELECT data FROM response_cache WHERE cache_key LIKE ?",
                ("/equities/earnings-calendar?date=%",),
            ).fetchall()
        except sqlite3.OperationalError:
            t2_rows = []

        matches = []
        seen_dates: set[str] = set()
        for row in t2_rows:
            records = json.loads(row["data"])
            if isinstance(records, dict):
                records = records.get("data", [])
            for rec in records:
                rec_code = str(rec.get("Code", ""))
                if rec_code == code:
                    ann_date = str(rec.get("Date", ""))[:10]
                    if ann_date not in seen_dates:
                        seen_dates.add(ann_date)
                        rec["Date"] = ann_date
                        matches.append(rec)

        matches.sort(key=lambda r: r.get("Date", ""), reverse=True)
        return {"count": len(matches), "data": matches}

    def _get_earnings_by_date_tier1(cache: CacheStore, norm_date: str) -> list[dict[str, Any]]:
        """Query equities_earnings_calendar Tier 1 for all entries on a specific date."""
        conn = cache._ensure_connection()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT data FROM equities_earnings_calendar WHERE date = ? ORDER BY code",
                (norm_date,),
            ).fetchall()
            return _normalize_earnings_records([json.loads(r["data"]) for r in rows])
        except sqlite3.OperationalError:
            return []


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
    """Retrieve daily stock bars (OHLC) with Tier 1 cache."""
    # 4桁コードを5桁に正規化（普通株式: 末尾0）
    cache_code = code + "0" if len(code) == 4 else code

    try:
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

    except (
        APIError,
        InvalidAPIKeyError,
        UserNotConfiguredError,
        DecryptionError,
        UserNotAllowedError,
    ) as e:
        return format_api_error(e)
