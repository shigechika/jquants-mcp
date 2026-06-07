"""Financial data tools for jquants-mcp."""

from __future__ import annotations

import logging
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Any

from fastmcp import FastMCP

from ..cache.store import CacheStore, TTL_24H, make_cache_key
from ..client import JQuantsClient
from ..exceptions import (
    TOOL_API_ERRORS,
    format_api_error,
)
from ..tool_annotations import READ_ONLY_API, READ_ONLY_CACHE
from ..validators import (
    collect_errors,
    display_code,
    float_or_none,
    make_validation_error_response,
    validate_code,
    validate_date,
)

logger = logging.getLogger(__name__)

# Per-share fields in fins_summary that need stock split adjustment
_SPLIT_ADJ_FIELDS = ("BPS", "EPS", "DivAnn")

# Fiscal period values surfaced via the derived FiscalPeriod field.
_VALID_FISCAL_PERIODS = ("1Q", "2Q", "3Q", "FY", "Other")

# fins_summary Tier 1 cache may store 19-char datetime for these fields; strip to YYYY-MM-DD.
_FY_DATE_FIELDS = ("CurFYEn", "CurFYSt", "NxtFYEn", "NxtFYSt")


def _derive_fiscal_period(row: dict[str, Any]) -> str | None:
    """Return the fiscal period label for a fins_summary row.

    Priority:
    1. ``CurPerType`` / legacy ``TypeOfCurrentPeriod`` if it already matches a
       recognised period code.
    2. Prefix of ``DocType`` / legacy ``TypeOfDocument`` for statements
       (``1Q``/``2Q``/``3Q``/``FY``/``OtherPeriod``).

    The ``"Other"`` label maps to ``OtherPeriodFinancialStatements_*`` —
    irregular reporting periods such as the 5-month statement issued when
    a company changes its fiscal year-end.

    Returns ``None`` for forecast revisions and any unparseable shape.
    """
    cur = row.get("CurPerType") or row.get("TypeOfCurrentPeriod") or ""
    cur = str(cur).strip()
    if cur in _VALID_FISCAL_PERIODS:
        return cur

    doc = str(row.get("DocType") or row.get("TypeOfDocument") or "")
    for prefix in ("1Q", "2Q", "3Q", "FY"):
        if doc.startswith(f"{prefix}Financial"):
            return prefix
    if doc.startswith("OtherPeriodFinancial"):
        return "Other"
    return None


def _annotate_fiscal_period(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Inject ``FiscalPeriod`` into each row in-place and return the list."""
    for row in rows:
        row["FiscalPeriod"] = _derive_fiscal_period(row)
    return rows


def _parse_iso_date(value: str | None) -> date_cls | None:
    """Parse ``YYYYMMDD`` or ``YYYY-MM-DD`` into a date; None on empty/invalid."""
    if not value:
        return None
    try:
        return datetime.strptime(value.replace("-", ""), "%Y%m%d").date()
    except ValueError:
        return None


def _apply_split_adjustment(
    rows: list[dict[str, Any]],
    cache: CacheStore,
) -> tuple[list[dict[str, Any]], bool]:
    """Apply stock split adjustment to per-share financial fields.

    J-Quants AdjFactor is the split ratio on the day it occurred (e.g., 0.2
    for a 1:5 split), NOT a cumulative factor. To adjust historical per-share
    values, we multiply all AdjFactor values after the disclosure date to get
    the cumulative split factor, then multiply the per-share value by it.

    Example: 1:5 split on 2025-03-28 (AdjFactor=0.2)
      - BPS disclosed 2024-02-06 = 6000 -> AdjBPS = 6000 * 0.2 = 1200
      - BPS disclosed 2025-05-01 = 1200 -> AdjBPS = 1200 * 1.0 = 1200 (no split after)

    Note: This "splits after disc_date" logic is correct for EPS and BPS, which are
    already reported in post-split terms per Japanese GAAP retroactive adjustment.
    DivAnn is an exception: when a FY-end split occurs ~45 days before the annual
    results filing, J-Quants still stores DivAnn in pre-split per-share units.
    That additional correction is applied in get_dividend_yield_ranking and
    get_stock_briefing via CacheStore.get_split_factors_before_disc().

    Returns:
        Tuple of (adjusted rows, whether adjustment was applied).
    """
    if not rows:
        return rows, False

    code = rows[0].get("Code", "")
    if not code:
        return rows, False

    # Check if any split data exists for this code
    latest_adj = cache.get_latest_adj_factor(code)
    if latest_adj is None:
        return rows, False

    adjusted = False
    for row in rows:
        disc_date = row.get("DiscDate", row.get("disc_date", ""))
        if not disc_date:
            continue

        cum_factor = cache.get_cumulative_split_factor(code, disc_date)

        if abs(cum_factor - 1.0) < 1e-10:
            # No splits after this date — copy original values
            for field in _SPLIT_ADJ_FIELDS:
                val = row.get(field)
                if val is not None and val != "":
                    row[f"Adj{field}"] = val
            continue

        adjusted = True
        for field in _SPLIT_ADJ_FIELDS:
            val = row.get(field)
            if val is not None and val != "":
                try:
                    row[f"Adj{field}"] = round(float(val) * cum_factor, 2)
                except (ValueError, TypeError):
                    pass

    return rows, adjusted


def register(
    mcp: FastMCP,
    get_client: callable,
    get_cache: callable,
) -> None:
    """Register financial tools on the MCP server."""

    @mcp.tool(annotations=READ_ONLY_API)
    async def get_fins_summary(
        code: str | None = None,
        date: str | None = None,
    ) -> dict[str, Any]:
        """Use this first for any financial metric query (EPS, BPS, 売上, 利益, 配当, 業績予想). All plans.

        Returns quarterly financials: revenue, operating profit, net income, EPS/BPS/CF,
        dividends, and earnings forecasts. FiscalPeriod label: "1Q"/"2Q"/"3Q"/"FY"/"Other"/null.
        Either code or date must be specified.

        [Supported plans] Free / Light / Standard / Premium
        Note: Free plan data is delayed by 12 weeks.

        Args:
            code: Stock code (5 digits, e.g. 27800; 4-digit codes match ordinary shares only).
            date: Date (YYYYMMDD or YYYY-MM-DD). Returns financials disclosed on that date.
        """
        errors = collect_errors(validate_code(code), validate_date(date))
        if errors:
            return make_validation_error_response(errors)

        client: JQuantsClient = await get_client()
        cache: CacheStore = get_cache()

        # Tier 1 cache: when code is specified
        if code:
            return await _get_fins_summary_with_cache(client, cache, code, date)

        # Tier 2 cache when only date is specified
        params = {"code": code, "date": date}
        cache_key = make_cache_key("/fins/summary", params)
        cached = cache.get_response(cache_key)
        if cached is not None:
            return cached

        try:
            data = await client.get_all_pages("/fins/summary", params)
            _annotate_fiscal_period(data)
            for row in data:
                _normalize_fy_date_fields(row)
            result: dict[str, Any] = {"count": len(data), "data": data}
            result["split_adjustment"] = "not_applied"
            result["split_adjustment_reason"] = (
                "Split adjustment requires code parameter (date-only queries "
                "return multiple codes)."
            )
            cache.put_response(cache_key, result, ttl_seconds=TTL_24H)
            return result
        except TOOL_API_ERRORS as e:
            return format_api_error(e)

    @mcp.tool(annotations=READ_ONLY_API)
    async def get_fins_details(
        code: str | None = None,
        date: str | None = None,
    ) -> dict[str, Any]:
        """Use only when individual BS/PL/CF line items are needed (Premium plan only).

        For common financial metrics (EPS, BPS, revenue, profit, dividends, 業績予想), use
        ``get_fins_summary`` instead — it is faster (cached) and available to all plans.

        Returns detailed financial statement line items: balance sheet (BS), income
        statement (PL), and cash flow (CF), supporting both Japanese GAAP and IFRS.
        Either 'code' or 'date' must be specified.

        [Supported plans] Premium

        Args:
            code: Stock code (5 digits, e.g. 27800; 4-digit codes match ordinary shares only)
            date: Date (YYYY-MM-DD). Returns financial statements disclosed on that date.
        """
        errors = collect_errors(validate_code(code), validate_date(date))
        if errors:
            return make_validation_error_response(errors)

        client: JQuantsClient = await get_client()
        cache: CacheStore = get_cache()

        params = {"code": code, "date": date}
        cache_key = make_cache_key("/fins/details", params)
        cached = cache.get_response(cache_key)
        if cached is not None:
            return cached

        try:
            data = await client.get_all_pages("/fins/details", params)
            result = {"count": len(data), "data": data}
            cache.put_response(cache_key, result, ttl_seconds=TTL_24H)
            return result
        except TOOL_API_ERRORS as e:
            return format_api_error(e)

    @mcp.tool(annotations=READ_ONLY_API)
    async def get_fins_dividend(
        code: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve cash dividend data.

        Returns dividend data including record date, ex-dividend date, dividend amount
        (forecast and actual), expected payment start date, and commemorative/special dividends.

        [Supported plans] Premium

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
        cache_key = make_cache_key("/fins/dividend", params)
        cached = cache.get_response(cache_key)
        if cached is not None:
            return cached

        try:
            data = await client.get_all_pages("/fins/dividend", params)
            result = {"count": len(data), "data": data}
            cache.put_response(cache_key, result, ttl_seconds=TTL_24H)
            return result
        except TOOL_API_ERRORS as e:
            return format_api_error(e)

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def get_earnings_results_this_week(
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Earnings results actually disclosed in a date window, grouped by day (今週の決算実績). All plans.

        Use for 今週発表された決算, 直近の決算結果, この期間に出た決算の中身, 決算実績一覧,
        earnings results this week. Default window = today-7d .. today (results are
        disclosed in the past). Each filing carries headline P&L (売上/営業利益/純利益/EPS),
        annual dividend, and — for quarterly filings — progress vs the full-year forecast.
        For the upcoming SCHEDULE use get_earnings_this_week; for one stock's full
        financials use get_fins_summary.

        [Supported plans] Free / Light / Standard / Premium (cache-only, no API call)
        Note: Free plan results are delayed 12 weeks, so the recent window is empty for Free.

        Args:
            date_from: Window start inclusive (YYYYMMDD or YYYY-MM-DD). Defaults to date_to - 7 days.
            date_to:   Window end inclusive (YYYYMMDD or YYYY-MM-DD). Defaults to today.
        """
        errors = collect_errors(
            validate_date(date_from, "date_from"),
            validate_date(date_to, "date_to"),
        )
        if errors:
            return make_validation_error_response(errors)

        end = _parse_iso_date(date_to) or date_cls.today()
        start = _parse_iso_date(date_from) or (end - timedelta(days=7))
        if end < start:
            return make_validation_error_response(["`date_to` must be on or after `date_from`."])

        f_iso, t_iso = start.isoformat(), end.isoformat()
        cache: CacheStore = get_cache()

        records = cache.get_fins_disclosures_in_range(f_iso, t_iso)
        name_map = cache.get_name_map() if records else {}
        sector_map = cache.get_sector_map() if records else {}

        by_date: dict[str, list[dict[str, Any]]] = {}
        total = 0
        for rec in records:
            # Only actual financial statements (1Q/2Q/3Q/FY/Other); forecast and
            # dividend revisions return None here and are skipped.
            fp = _derive_fiscal_period(rec)
            if fp is None:
                continue
            raw_code = str(rec.get("Code") or "")
            if not raw_code:
                continue
            day = str(rec.get("DiscDate") or rec.get("disc_date") or "")[:10]
            if not day:
                continue
            info = sector_map.get(raw_code, {})

            net_profit = float_or_none(rec.get("NP"))
            # Quarterly filings carry the full-year forecast (FNP); progress = how
            # much of the forecast the cumulative actual has reached.
            fcast_np = float_or_none(rec.get("FNP"))
            progress = None
            if fp in ("1Q", "2Q", "3Q") and net_profit is not None and fcast_np not in (None, 0):
                progress = round(net_profit / fcast_np * 100, 1)

            by_date.setdefault(day, []).append(
                {
                    "code": display_code(raw_code),
                    "name": name_map.get(raw_code),
                    "sector": info.get("s33_name") or None,
                    "market": info.get("mkt_name") or None,
                    "fiscal_period": fp,
                    "fiscal_year_end": str(rec.get("CurFYEn") or "")[:10] or None,
                    "sales": float_or_none(rec.get("Sales")),
                    "operating_profit": float_or_none(rec.get("OP")),
                    "net_profit": net_profit,
                    "eps": float_or_none(rec.get("EPS")),
                    "div_ann": float_or_none(rec.get("DivAnn")),
                    "forecast_progress_pct": progress,
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


# ------------------------------------------------------------------
# Tier 1 cache: per-code cache for financial summary
# ------------------------------------------------------------------


def _normalize_disc_date(row: dict[str, Any]) -> None:
    """Normalize DiscDate in-place to YYYY-MM-DD, stripping any time suffix."""
    raw = row.get("DiscDate")
    if isinstance(raw, str) and len(raw) > 10:
        row["DiscDate"] = raw[:10]


def _normalize_fy_date_fields(row: dict[str, Any]) -> None:
    """Normalize fiscal year date fields in-place to YYYY-MM-DD, stripping any time suffix."""
    for field in _FY_DATE_FIELDS:
        val = row.get(field)
        if isinstance(val, str) and len(val) > 10:
            row[field] = val[:10]


def _dedup_key(row: dict[str, Any]) -> str:
    """Return a dedup key that is stable regardless of DiscDate time-suffix format."""
    disc = (row.get("DiscDate") or "")[:10]
    return f"{row.get('Code')}_{disc}_{row.get('DiscNo', '')}"


async def _get_fins_summary_with_cache(
    client: JQuantsClient,
    cache: CacheStore,
    code: str,
    date: str | None,
) -> dict[str, Any]:
    """Retrieve financial summary data with Tier 1 cache."""
    try:
        # Check cache
        key_filter = {"code": code}
        if date:
            key_filter["disc_date"] = date

        cached_data = cache.get_rows(
            "fins_summary",
            key_filter=key_filter,
            date_column="disc_date",
        )

        if cached_data and date:
            # Apply split adjustment even for cached data
            adjusted, _ = _apply_split_adjustment(cached_data, cache)
            _annotate_fiscal_period(adjusted)
            for row in adjusted:
                _normalize_disc_date(row)
                _normalize_fy_date_fields(row)
            return {"count": len(adjusted), "data": adjusted, "source": "cache"}

        # Fetch from API
        params: dict[str, Any] = {"code": code}
        if date:
            params["date"] = date

        api_data = await client.get_all_pages("/fins/summary", params)

        if api_data:
            # Store in cache (keyed on DiscDate)
            cache.put_rows(
                "fins_summary",
                api_data,
                key_columns=["Code", "DiscDate"],
            )

        # Merge (when date is not specified, combine cache and API)
        if not date and cached_data:
            seen_keys: set[str] = set()
            merged: list[dict[str, Any]] = []
            for row in api_data:
                _normalize_disc_date(row)
                key = _dedup_key(row)
                if key not in seen_keys:
                    seen_keys.add(key)
                    merged.append(row)
            for row in cached_data:
                _normalize_disc_date(row)
                key = _dedup_key(row)
                if key not in seen_keys:
                    seen_keys.add(key)
                    merged.append(row)
            merged.sort(key=lambda r: r.get("DiscDate", ""))
            source = "cache+api"
        else:
            merged = api_data
            source = "api"

        merged, split_adjusted = _apply_split_adjustment(merged, cache)
        _annotate_fiscal_period(merged)
        for row in merged:
            _normalize_fy_date_fields(row)
        result = {"count": len(merged), "data": merged, "source": source}
        if not split_adjusted and any(
            r.get(f) not in (None, "") for r in merged for f in _SPLIT_ADJ_FIELDS
        ):
            result["split_adjustment"] = "not_applied"
            result["split_adjustment_reason"] = (
                "No AdjFactor data in equities_bars_daily cache for this code. "
                "Fetch daily bars first to enable split adjustment."
            )
        return result

    except TOOL_API_ERRORS as e:
        return format_api_error(e)
