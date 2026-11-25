"""Equity-related tools for jquants-mcp."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import date as date_cls
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from mcp.server.mcpserver import MCPServer

from ..cache.publication import JST, publication_ttl
from ..cache.store import ENDPOINT_TTL, CacheStore, TTL_24H, TTL_7D, make_cache_key
from ..client import JQuantsClient
from ..exceptions import (
    TOOL_API_ERRORS,
    APIError,
    AuthenticationError,
    RateLimitError,
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
    """Normalize Date field to YYYY-MM-DD in earnings calendar records (in-place)."""
    for rec in records:
        raw = rec.get("Date")
        if raw is not None:
            rec["Date"] = str(raw)[:10]
    return records


def _parse_iso_date(value: str | None) -> date_cls | None:
    """Parse ``YYYYMMDD`` or ``YYYY-MM-DD`` into a date; None on empty/invalid."""
    if not value:
        return None
    try:
        return datetime.strptime(value.replace("-", ""), "%Y%m%d").date()
    except ValueError:
        return None


def register(
    mcp: MCPServer,
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
        except TOOL_API_ERRORS as e:
            return format_api_error(e)

    @mcp.tool(annotations=READ_ONLY_API)
    async def get_equities_bars_daily(
        code: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Per-stock or per-day OHLC bars (日足株価). All plans.

        Use for 株価・日足・OHLC・終値・出来高・調整株価 (AdjC/AdjO etc.), 権利落ち (ExRT)
        queries. MktCap (時価総額): market cap in millions of yen (unadjusted close x listed
        shares, corporate-action aware); null for ETF/ETN and non-trading days. J-Quants
        stops returning MktCap from this endpoint on 2026-12-28: rows fetched after that
        have no MktCap key and a refetch can drop it from cached rows, so use
        get_equities_valuation instead (its MktCap excludes treasury shares, so values
        differ). ExRT:
        ex-rights corporate action on that date (1=split/free share allotment, 2=merger,
        3=rights issue); null when none. For multi-stock bulk downloads use get_bulk_list
        (date-only queries are very slow). code only → full history; code+range → period;
        date only → all stocks on that date. partial=true: the API failed and only
        the (incomplete) cached rows were returned.

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

        # code + date range: use the Tier 1 cache for incremental fetch
        if code and (date_from or date_to or date):
            return await _get_bars_daily_with_cache(client, cache, code, date, date_from, date_to)

        # date only (all stocks): use the Tier 2 cache
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
        except TOOL_API_ERRORS as e:
            return format_api_error(e)

    @mcp.tool(annotations=READ_ONLY_API)
    async def get_equities_valuation(
        code: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Daily valuation metrics per stock (バリュエーション指標). All plans.

        Use for PER・PBR・ROE・EPS・BPS・予想PER・時価総額 (MktCap) time series or a
        one-day cross-section. EPS/BPS: yen, trailing actuals; FwdEPS/FwdPER/FwdROE:
        from the current-period forecast; ROE/FwdROE are fractions (0.115 = 11.5%);
        MktCap: millions of yen with treasury shares excluded (a different definition
        from the MktCap get_equities_bars_daily returns until 2026-12-28). Per-share
        values are as of each date, not split-adjusted. All fields null for ETF/ETN/REIT.
        code only → full history; code+range → period; date only → all stocks on that
        date. partial=true: the API failed and only the (incomplete) cached rows were
        returned.

        [Supported plans] Free / Light / Standard / Premium (API fallback on cache miss)
        Retention: Free=2y (12w delay), Light=5y, Standard=10y, Premium=all.

        Args:
            code: Stock code, 4 or 5 characters (e.g. 7203 / 72030, 285A / 285A0); a
                4-character code matches the ordinary share only.
            date: Single date (YYYYMMDD or YYYY-MM-DD).
            date_from: Range start inclusive (YYYYMMDD or YYYY-MM-DD); needs code.
            date_to: Range end inclusive (YYYYMMDD or YYYY-MM-DD); needs code.
        """
        if code is None and date is None:
            return make_validation_error_response(["Either 'code' or 'date' must be specified."])
        if code is None and (date_from or date_to):
            return make_validation_error_response(["'date_from' / 'date_to' need 'code'."])
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

        # code + date range: Tier 1 cache with the shared completeness check
        if code and (date_from or date_to or date):
            return await _get_code_series_with_cache(
                client,
                cache,
                table="equities_valuation",
                endpoint="/equities/valuation",
                head_name="valuation_head",
                code=code,
                date=date,
                date_from=date_from,
                date_to=date_to,
            )

        # code only (full history) or date only (all stocks): Tier 2 cache, keyed
        # by plan so one plan's window is never served to another
        params = {"code": code, "date": date, "from": date_from, "to": date_to}
        cache_key = make_cache_key(
            "/equities/valuation", {**params, "plan": cache.effective_plan()}
        )
        cached = cache.get_response(cache_key)
        if cached is not None:
            # The plan window moves daily: drop rows that left it since caching
            return _clip_to_plan(cached, cache.plan_bounds())

        try:
            data = await client.get_all_pages("/equities/valuation", params)
            result = {"count": len(data), "data": data}
            # An empty answer (e.g. before the day is published) must not stick for 24h
            if data:
                # A past day's cross-section never changes. A plan with a delay
                # ceiling (Free) never sees today's row, but its ceiling moves at
                # midnight, so its answers last until then. Anything that can
                # still gain today's row expires at the next publication.
                today = datetime.now(JST).date().isoformat()
                _, plan_max = cache.plan_bounds()
                if date and (_to_iso(date) or "") < today:
                    ttl = TTL_24H
                elif plan_max is not None:
                    ttl = _seconds_until_midnight()
                else:
                    ttl = publication_ttl(cache, "/equities/valuation", data)
                cache.put_response(cache_key, result, ttl_seconds=ttl)
            return result
        except TOOL_API_ERRORS as e:
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
        except TOOL_API_ERRORS as e:
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

        # Do not cache: this is real-time data
        try:
            data = await client.get_all_pages("/equities/bars/daily/am", {"code": code})
            return {"count": len(data), "data": data}
        except TOOL_API_ERRORS as e:
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
        except TOOL_API_ERRORS as e:
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
        Light+: all listed issues incl. REITs, any fiscal year-end (daily_fetch
        sweeps /fins/earnings-date). Free: falls back to March/September
        fiscal-year issues only (the older /equities/earnings-calendar), since
        the newer endpoint's Free-tier window excludes the last 12 weeks by
        publication date.
        No-argument query defaults to a -7/+14 day window around today. The
        Light+ all-issues coverage described above assumes daily_fetch's
        /fins/earnings-date sweep has already populated the cache; a
        same-plan live-fallback still queries the narrower legacy endpoint
        (jquants-mcp#621).
        Falls back to one live fetch on a cache miss (code queries always;
        date queries only for today/future dates, #523; the no-argument query
        when nothing is scheduled from today onwards, #536).

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
            result = _search_earnings_by_code(cache, code)
            if result["count"] == 0 and await _refresh_earnings_calendar_live(client, cache):
                result = _search_earnings_by_code(cache, code)
            return result

        # Date filter: Tier 1 first, fallback to Tier 2 response cache
        if date is not None:
            norm_date = date if "-" in date else f"{date[:4]}-{date[4:6]}-{date[6:]}"
            t1_rows = _get_earnings_by_date_tier1(cache, norm_date)
            if t1_rows:
                return {"count": len(t1_rows), "data": t1_rows}
            date_key = date.replace("-", "")
            cached = cache.get_response(
                make_cache_key("/equities/earnings-calendar", {"date": date_key})
            )
            if cached is None:
                # daily_fetch wrote "?date=" keys before #620; published caches
                # keep them for their 90-day TTL
                cached = cache.get_response(f"/equities/earnings-calendar?date={date_key}")
            if cached is not None:
                recs = cached if isinstance(cached, list) else cached.get("data", [])
                recs = _normalize_earnings_records(recs)
                return {"count": len(recs), "data": recs}
            if norm_date >= date_cls.today().isoformat() and (
                await _refresh_earnings_calendar_live(client, cache)
            ):
                # Upstream only carries near-term announcements, so a live
                # refresh can only help for today/future dates (#523).
                t1_rows = _get_earnings_by_date_tier1(cache, norm_date)
                if t1_rows:
                    return {"count": len(t1_rows), "data": t1_rows}
            return {"count": 0, "data": [], "message": f"No data for date {date}."}

        # No filter: Tier 1 first, exactly like the code and date branches.
        # It used to read the Tier 2 blob straight away, and that blob carries a
        # 90-day TTL — so this branch could serve a snapshot months older than
        # the rows already sitting in Tier 1 (#536). Upstream only publishes
        # near-term announcements; Tier 1 accumulates them, so the union across
        # a bounded window around today is the freshest and fullest answer.
        today = date_cls.today()
        window_from = (today - timedelta(days=_EARNINGS_WINDOW_BACK_DAYS)).isoformat()
        window_to = (today + timedelta(days=_EARNINGS_WINDOW_AHEAD_DAYS)).isoformat()

        t1_rows = _normalize_earnings_records(cache.get_earnings_in_range(window_from, window_to))
        if not any(str(r.get("Date", ""))[:10] >= today.isoformat() for r in t1_rows):
            # Nothing scheduled from today onwards is the signature of a stale
            # calendar, not of a quiet market — refresh before answering.
            if await _refresh_earnings_calendar_live(client, cache):
                t1_rows = _normalize_earnings_records(
                    cache.get_earnings_in_range(window_from, window_to)
                )
        if t1_rows:
            return {"count": len(t1_rows), "data": t1_rows}

        # Tier 1 empty (e.g. before the first daily_fetch after an upgrade).
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
            # 24h, not 90d: this blob is only a fallback for an empty Tier 1,
            # and a long TTL is what let it go stale unnoticed.
            cache.put_response(cache_key, result, ttl_seconds=TTL_24H)
            return result
        except TOOL_API_ERRORS as e:
            return format_api_error(e)

    @mcp.tool(annotations=READ_ONLY_API)
    async def get_earnings_this_week(
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Companies reporting earnings in a date window, grouped by day (今週の決算予定). All plans.

        Use for 今週決算がある銘柄, 直近の決算スケジュール, この期間に決算発表する会社一覧,
        決算前スクリーニング, earnings this week. Default window = today .. today+7d.
        Enriches each company with its name and 33-sector from the equities master.
        For a single stock's next earnings date use get_equities_earnings_calendar(code=...).

        [Supported plans] Free / Light / Standard / Premium (cache-first; falls back
        to one live fetch when the today-or-later part of the window has no cached
        rows, or today's rows are missing, #523)

        Args:
            date_from: Window start inclusive (YYYYMMDD or YYYY-MM-DD). Defaults to today.
            date_to:   Window end inclusive (YYYYMMDD or YYYY-MM-DD). Defaults to date_from + 7 days.
        """
        errors = collect_errors(
            validate_date(date_from, "date_from"),
            validate_date(date_to, "date_to"),
        )
        if errors:
            return make_validation_error_response(errors)

        start = _parse_iso_date(date_from) or date_cls.today()
        end = _parse_iso_date(date_to) or (start + timedelta(days=7))
        if end < start:
            return make_validation_error_response(["`date_to` must be on or after `date_from`."])

        f_iso, t_iso = start.isoformat(), end.isoformat()
        cache: CacheStore = get_cache()

        records = cache.get_earnings_in_range(f_iso, t_iso)
        today_iso = date_cls.today().isoformat()
        if t_iso >= today_iso:
            # The nightly cron may have run before the upstream 19:00 JST
            # update, so today's list can be missing even when OTHER days in
            # the window are already cached (#523) — check the forward part of
            # the window and today's date specifically, not the whole union.
            cached_dates = {str(r.get("Date", ""))[:10] for r in records}
            forward_from = max(f_iso, today_iso)
            forward_empty = not any(d >= forward_from for d in cached_dates)
            today_missing = f_iso <= today_iso <= t_iso and today_iso not in cached_dates
            if forward_empty or today_missing:
                client: JQuantsClient = await get_client()
                if await _refresh_earnings_calendar_live(client, cache):
                    records = cache.get_earnings_in_range(f_iso, t_iso)
        # Enrich with canonical name/sector from the master (fields in the
        # calendar row itself are the field-normalized short names CoName /
        # SectorNm / FQ / FY; fall back to the master when absent).
        name_map = cache.get_name_map() if records else {}
        sector_map = cache.get_sector_map() if records else {}

        by_date: dict[str, list[dict[str, Any]]] = {}
        total = 0
        for rec in records:
            raw_code = str(rec.get("Code") or "")
            if not raw_code:
                continue
            day = str(rec.get("Date") or "")[:10]
            info = sector_map.get(raw_code, {})
            by_date.setdefault(day, []).append(
                {
                    "code": display_code(raw_code),
                    "name": rec.get("CoName") or name_map.get(raw_code) or None,
                    "sector": rec.get("SectorNm") or info.get("s33_name") or None,
                    "market": info.get("mkt_name") or rec.get("Section") or None,
                    "fiscal_quarter": rec.get("FQ"),
                    "fiscal_year_end": rec.get("FY"),
                }
            )
            total += 1

        days = [
            {
                "date": day,
                "count": len(by_date[day]),
                "companies": sorted(by_date[day], key=lambda r: r["code"]),
            }
            for day in sorted(by_date)
        ]
        return {
            "count": total,
            "date_from": f_iso,
            "date_to": t_iso,
            "days": days,
        }

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

        # Plan-agnostic read (mirrors get_name_map/get_sector_map): equities_master
        # is a reference table, so plan gating would incorrectly suppress a code
        # whose only cached row falls inside a lower-tier plan's embargo/retention
        # window. Already deduplicated to the latest row per code.
        rows = cache.get_all_equities_master_rows()

        query_lower = query.lower()
        matches: list[dict[str, Any]] = []
        for row in rows:
            code = str(row.get("Code") or "")
            if not code:
                continue
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
            # equities_master stores the normalised short field names (see
            # get_sector_map): MktNm/Mkt for market, S33Nm for sector — not
            # the long-form names used below, which never matched any stored
            # field and left these keys permanently absent from the output.
            market = row.get("MktNm") or row.get("Mkt")
            if market:
                entry["market"] = str(market)
            sector = row.get("S33Nm")
            if sector:
                entry["sector"] = sector
            matches.append(entry)

        matches.sort(key=lambda r: r["code"])
        return {"count": len(matches), "query": query, "data": matches}

    _EARNINGS_LIVE_REFRESH_TTL_S = 1800
    # Single-flight gate: three tools share this fallback, so without it a
    # burst of concurrent requests each fetches the same upstream payload and
    # writes it back. The suppression marker below only helps AFTER the first
    # fetch completes; this covers the window before that (#537).
    _earnings_refresh_lock = asyncio.Lock()
    # Window (days) for the no-filter query. On Light+ plans daily_fetch's
    # /fins/earnings-date sweep covers all listed issues (not just the old
    # March/September-fiscal-year subset), so a symmetric +/-90-day window
    # would now hold several thousand rows (~1MB payload). Narrowed to match
    # the sweep's own bare-key cache window (see daily_fetch.py's
    # _fetch_earnings_calendar_sweep near_back/near_ahead).
    _EARNINGS_WINDOW_BACK_DAYS = 7
    _EARNINGS_WINDOW_AHEAD_DAYS = 14

    async def _refresh_earnings_calendar_live(client: JQuantsClient, cache: CacheStore) -> bool:
        """Fetch the latest earnings calendar and upsert it into the Tier 1 table.

        The upstream dataset is published around 19:00 JST carrying the NEXT
        business day's announcements, while the nightly ``daily_fetch`` cron may
        run earlier in the evening — in that case the cache never holds "today's"
        announcements until after they have happened (#523). This cache-miss
        fallback closes the gap at query time. A short-TTL global Tier 2 marker
        suppresses repeat fetches for 30 minutes after a successful — possibly
        empty — fetch (weekends, holidays); failed fetches set no marker and
        are retried on the next call.

        Returns True when new records were fetched and upserted.
        """
        marker = make_cache_key("/equities/earnings-calendar", {"live_refresh": "1"})
        if cache.get_response(marker) is not None:
            return False
        async with _earnings_refresh_lock:
            # Re-check inside the gate: a caller that queued behind the winner
            # would otherwise repeat the fetch it was waiting for.
            if cache.get_response(marker) is not None:
                return False
            try:
                data = await client.get_all_pages("/equities/earnings-calendar")
            except (*TOOL_API_ERRORS, RateLimitError, AuthenticationError) as e:
                # Best-effort side fetch: never fail an otherwise cache-served
                # request. No marker is set, so the next call retries.
                logger.warning("Live earnings-calendar refresh failed: %s", e)
                return False
            cache.put_response(
                marker, {"refreshed": True}, ttl_seconds=_EARNINGS_LIVE_REFRESH_TTL_S
            )
            records = _normalize_earnings_records(data)
            if not records:
                return False
            cache.put_rows("equities_earnings_calendar", records, key_columns=["Code", "Date"])
            return True

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

        # Tier 2 fallback: scan date-keyed response_cache entries, in the current
        # make_cache_key shape and the "?date=" shape daily_fetch wrote before #620.
        # A day rewritten under the current key supersedes its legacy entry, which
        # may still list announcements that have since been withdrawn.
        try:
            key_rows = conn.execute(
                "SELECT cache_key, data FROM response_cache WHERE cache_key LIKE ? OR cache_key LIKE ?",
                ("/equities/earnings-calendar|date=%", "/equities/earnings-calendar?date=%"),
            ).fetchall()
        except sqlite3.OperationalError:
            key_rows = []
        current_days = {
            r["cache_key"].rsplit("=", 1)[-1] for r in key_rows if "|date=" in r["cache_key"]
        }
        t2_rows = [
            r
            for r in key_rows
            if "|date=" in r["cache_key"] or r["cache_key"].rsplit("=", 1)[-1] not in current_days
        ]

        matches = []
        seen_dates: set[str] = set()
        for row in t2_rows:
            try:
                records = json.loads(row["data"])
            except (TypeError, ValueError):
                # Skip corrupted cache rows instead of failing the whole query.
                logger.warning("Skipping corrupted earnings-calendar cache row")
                continue
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
# Tier 1 cache: incremental fetch of daily OHLC bars
# ------------------------------------------------------------------


def _seconds_until_midnight(now: datetime | None = None) -> int:
    """Seconds until the plan date window next moves; at least 60.

    That is the earlier of the next local midnight (the clock
    ``_plan_date_bounds`` uses) and the next JST midnight (the exchange's day,
    which J-Quants' own delay ceiling follows). They differ on a UTC host.
    """
    now = (now or datetime.now(JST)).astimezone()
    local_midnight = datetime.combine(
        now.date() + timedelta(days=1), datetime.min.time(), tzinfo=now.tzinfo
    )
    now_jst = now.astimezone(JST)
    jst_midnight = datetime.combine(
        now_jst.date() + timedelta(days=1), datetime.min.time(), tzinfo=JST
    )
    seconds = min(local_midnight - now, jst_midnight - now_jst).total_seconds()
    return max(60, int(seconds))


def _clip_to_plan(result: Any, bounds: tuple[str | None, str | None]) -> Any:
    """Drop rows whose Date is outside the plan's (min, max) window from a cached answer."""
    lo, hi = bounds
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        return result
    if lo is None and hi is None:
        return result
    rows = [
        r
        for r in result["data"]
        if not isinstance(r, dict)
        or (
            (lo is None or str(r.get("Date", ""))[:10] >= lo)
            and (hi is None or str(r.get("Date", ""))[:10] <= hi)
        )
    ]
    if len(rows) == len(result["data"]):
        return result
    return {**result, "count": len(rows), "data": rows}


def _to_iso(d: str | None) -> str | None:
    """Convert a validated YYYYMMDD or YYYY-MM-DD date to YYYY-MM-DD."""
    parsed = _parse_iso_date(d)
    return parsed.isoformat() if parsed else None


# How long a per-code head record ("no bars before X") is trusted. Short, so a
# wrong record (e.g. an upstream outage that hid early rows) expires quickly.
_HEAD_RECORD_TTL = TTL_7D


def _head_floor(record: Any, lo: str | None) -> str | None:
    """First bar date from a head record, if it covers a request starting at ``lo``.

    A record says the API returned nothing in [asked_from, first). It applies
    only when the request starts at or after asked_from (an unbounded ask covers
    every request); days on or after ``first`` are never excused by it.
    """
    if not isinstance(record, dict) or not isinstance(record.get("first"), str):
        return None
    asked_from = record.get("asked_from")
    if asked_from is None or (lo is not None and lo >= asked_from):
        return record["first"]
    return None


async def _expected_bar_days(
    client: JQuantsClient,
    cache: CacheStore,
    lo: str | None,
    hi: str,
    first_bar: str | None,
    cached_dates: set[str],
    resolve_until: str | None = None,
) -> list[str] | None:
    """Trading days in [lo, hi] that must have a cached bar, or None if unknowable.

    Days before ``first_bar`` are dropped. When a missing day was only guessed
    to be a business day (no calendar row), the calendar for the missing span is
    loaded once and the expectation recomputed, so a weekday holiday cannot keep
    the range incomplete. A failed calendar load keeps the weekday guess.
    ``resolve_until`` limits that load to missing days on or before it, for
    callers whose decision cannot depend on later days.
    """

    def compute(guessed: set[str]) -> list[str] | None:
        days = cache.expected_trading_days(lo, hi, guessed)
        if days is not None and first_bar:
            days = [d for d in days if d >= first_bar]
        return days

    guessed: set[str] = set()
    expected = compute(guessed)
    if not expected:
        return expected
    unsure = [
        d
        for d in expected
        if d not in cached_dates and d in guessed and (resolve_until is None or d <= resolve_until)
    ]
    if not unsure:
        return expected
    try:
        response = await client.get("/markets/calendar", {"from": min(unsure), "to": max(unsure)})
    except TOOL_API_ERRORS as e:
        logger.info("Calendar load for bar completeness failed: %s", e)
        return expected
    rows = response.get("data", []) if isinstance(response, dict) else []
    if not rows:
        return expected
    cache.put_rows("markets_calendar", rows, key_columns=["Date"])
    return compute(set())


# Hook run on a non-empty API batch before it is stored: receives
# (api_data, full_fetch, cache_code) and returns (api_data, drop_cached).
_AfterFetch = Callable[
    [list[dict[str, Any]], bool, str], Awaitable[tuple[list[dict[str, Any]], bool]]
]


async def _get_bars_daily_with_cache(
    client: JQuantsClient,
    cache: CacheStore,
    code: str,
    date: str | None,
    date_from: str | None,
    date_to: str | None,
) -> dict[str, Any]:
    """Retrieve daily stock bars (OHLC) with Tier 1 cache and split detection."""

    async def refetch_on_split(
        api_data: list[dict[str, Any]], full_fetch: bool, cache_code: str
    ) -> tuple[list[dict[str, Any]], bool]:
        # Scan the whole batch, not just the last row (AdjFactor is a per-date
        # event flag, not a running value — see jquants-mcp#597).
        split_date = cache.detect_split_in_batch(cache_code, api_data)
        if not split_date:
            return api_data, False
        # Split detected -> invalidate the cache and refetch all rows
        cache.invalidate_rows("equities_bars_daily", {"code": cache_code})
        logger.info("株式分割検知、キャッシュ再取得: code=%s (発生日: %s)", code, split_date)
        if not full_fetch:
            # Only a tail was fetched: refetch the whole requested range
            # (a full-range answer is already in hand otherwise)
            params_full = {"code": code}
            if date_from:
                params_full["from"] = date_from
            if date_to:
                params_full["to"] = date_to
            api_data = await client.get_all_pages("/equities/bars/daily", params_full)
        return api_data, True

    return await _get_code_series_with_cache(
        client,
        cache,
        table="equities_bars_daily",
        endpoint="/equities/bars/daily",
        head_name="bars_daily_head",
        code=code,
        date=date,
        date_from=date_from,
        date_to=date_to,
        adj_factor_key="AdjFactor",
        after_fetch=refetch_on_split,
    )


async def _get_code_series_with_cache(
    client: JQuantsClient,
    cache: CacheStore,
    *,
    table: str,
    endpoint: str,
    head_name: str,
    code: str,
    date: str | None,
    date_from: str | None,
    date_to: str | None,
    adj_factor_key: str | None = None,
    after_fetch: _AfterFetch | None = None,
) -> dict[str, Any]:
    """Retrieve a per-stock daily series (one row per trading day) with Tier 1 cache.

    The cache alone answers a range only when every expected trading day in it
    is present (jquants-mcp#625). When just the tail is missing, only that tail
    is fetched; any other gap refetches the whole range. API rows win over
    cached rows for the same date. A single ``date`` always asks the API, so
    values restated after it was cached are picked up.

    Days that will never get a row must not keep a range incomplete forever:
    weekday holidays are resolved by loading markets_calendar on demand, and
    days before the first row the API returned (listing date, or the API's
    retention floor) are dropped using a short-lived per-code head record.
    """
    # Normalize 4-digit codes to 5 digits (ordinary shares: trailing 0)
    cache_code = code + "0" if len(code) == 4 else code
    # The cache stores ISO dates; the tool also accepts YYYYMMDD
    lo = _to_iso(date or date_from)
    hi = _to_iso(date or date_to)
    head_key = make_cache_key(head_name, {"code": cache_code, "plan": cache.effective_plan()})

    try:
        # Fetch existing data from the cache (look up by 5-digit code)
        cached_data = cache.get_rows(
            table,
            key_filter={"code": cache_code},
            date_from=lo,
            date_to=hi,
        )

        # Request from the API (pass the original code and dates as-is)
        params: dict[str, Any] = {"code": code}
        if date:
            params["date"] = date
        if date_from:
            params["from"] = date_from
        if date_to:
            params["to"] = date_to

        # Check which dates are already cached
        cached_dates = cache.get_cached_dates(
            table,
            key_filter={"code": cache_code},
            date_from=lo,
            date_to=hi,
        )

        full_fetch = True
        covered = False
        if cached_dates and not date:
            first_bar = _head_floor(cache.get_response(head_key), lo)
            hi_eff = hi or date_cls.today().isoformat()
            latest_cached = max(cached_dates)
            expected = await _expected_bar_days(
                client,
                cache,
                lo or first_bar,
                hi_eff,
                first_bar,
                cached_dates,
                # Open-ended ranges refetch from latest_cached anyway, so days
                # after it cannot change the fetch
                resolve_until=None if hi else latest_cached,
            )
            # An empty or unknowable expectation proves nothing: ask the API
            if expected:
                covered = all(d in cached_dates for d in expected)
                # Open-ended ranges (no date_to) always refetch from the latest
                # cached date, as before, so a corrected AdjFactor there is seen
                if hi and covered:
                    logger.info("%s fully cached: code=%s (%d rows)", table, code, len(cached_data))
                    return {"count": len(cached_data), "data": cached_data, "source": "cache"}
                head = [d for d in expected if d <= latest_cached]
                if head and all(d in cached_dates for d in head):
                    # Contiguous up to latest_cached: fetch only the missing tail
                    params["from"] = latest_cached
                    full_fetch = False

        try:
            api_data = await client.get_all_pages(endpoint, params)
        except APIError:
            # Return cached data even on API failure, flagged when it is incomplete
            if cached_data:
                # A failed freshness check does not make a covered range partial
                partial = not (lo in cached_dates if date else covered)
                logger.info(
                    "API failed, returning cached %s: code=%s (%d rows, partial=%s)",
                    table,
                    code,
                    len(cached_data),
                    partial,
                )
                result = {"count": len(cached_data), "data": cached_data, "source": "cache"}
                if partial:
                    result["partial"] = True
                return result
            raise

        if full_fetch and not date and api_data:
            # Nothing exists between the asked lower bound and the first bar
            # returned (listing date or the API's retention floor)
            first = min(str(r.get("Date", ""))[:10] for r in api_data)
            if lo is None or lo < first:
                cache.put_response(
                    head_key, {"asked_from": lo, "first": first}, ttl_seconds=_HEAD_RECORD_TTL
                )

        if api_data:
            if after_fetch is not None:
                api_data, drop_cached = await after_fetch(api_data, full_fetch, cache_code)
                if drop_cached:
                    cached_data = []

            # Store in the cache
            cache.put_rows(
                table,
                api_data,
                key_columns=["Code", "Date"],
                adj_factor_key=adj_factor_key,
            )

        # Merge cached data and API data (dedup)
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

        # Sort by date
        merged.sort(key=lambda r: r.get("Date", ""))

        source = "cache+api" if cached_data and api_data else ("cache" if cached_data else "api")
        return {"count": len(merged), "data": merged, "source": source}

    except TOOL_API_ERRORS as e:
        return format_api_error(e)
