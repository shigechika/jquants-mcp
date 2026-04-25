"""Financial data tools for jquants-dat-mcp."""

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
from ..validators import (
    collect_errors,
    make_validation_error_response,
    validate_code,
    validate_date,
)

logger = logging.getLogger(__name__)

# Per-share fields in fins_summary that need stock split adjustment
_SPLIT_ADJ_FIELDS = ("BPS", "EPS", "DivAnn")


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
        """Retrieve financial summary data.

        Returns quarterly financial data including revenue, operating profit, net income,
        EPS, BPS, cash flow, dividends, and earnings forecasts.
        Either 'code' or 'date' must be specified.

        [Supported plans] Free / Light / Standard / Premium
        Note: Free plan data is delayed by 12 weeks.

        Args:
            code: Stock code (5 digits, e.g. 27800; 4-digit codes match ordinary shares only)
            date: Date (YYYYMMDD or YYYY-MM-DD). Returns financials disclosed on that date.
        """
        errors = collect_errors(validate_code(code), validate_date(date))
        if errors:
            return make_validation_error_response(errors)

        client: JQuantsClient = await get_client()
        cache: CacheStore = get_cache()

        # Tier 1 キャッシュ: code 指定時
        if code:
            return await _get_fins_summary_with_cache(client, cache, code, date)

        # date のみ指定時は Tier 2 キャッシュ
        params = {"code": code, "date": date}
        cache_key = make_cache_key("/fins/summary", params)
        cached = cache.get_response(cache_key)
        if cached is not None:
            return cached

        try:
            data = await client.get_all_pages("/fins/summary", params)
            result: dict[str, Any] = {"count": len(data), "data": data}
            result["split_adjustment"] = "not_applied"
            result["split_adjustment_reason"] = (
                "Split adjustment requires code parameter (date-only queries "
                "return multiple codes)."
            )
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
    async def get_fins_details(
        code: str | None = None,
        date: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve detailed financial statements (BS/PL/CF).

        Returns detailed financial statement data including balance sheet (BS) and income
        statement (PL) line items, supporting both Japanese GAAP and IFRS.
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
        except (
            APIError,
            InvalidAPIKeyError,
            UserNotConfiguredError,
            DecryptionError,
            UserNotAllowedError,
        ) as e:
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
        except (
            APIError,
            InvalidAPIKeyError,
            UserNotConfiguredError,
            DecryptionError,
            UserNotAllowedError,
        ) as e:
            return format_api_error(e)


# ------------------------------------------------------------------
# Tier 1 キャッシュ: 財務サマリーの銘柄別キャッシュ
# ------------------------------------------------------------------


async def _get_fins_summary_with_cache(
    client: JQuantsClient,
    cache: CacheStore,
    code: str,
    date: str | None,
) -> dict[str, Any]:
    """Retrieve financial summary data with Tier 1 cache."""
    try:
        # キャッシュ確認
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
            return {"count": len(adjusted), "data": adjusted, "source": "cache"}

        # API から取得
        params: dict[str, Any] = {"code": code}
        if date:
            params["date"] = date

        api_data = await client.get_all_pages("/fins/summary", params)

        if api_data:
            # キャッシュに保存（DiscDate をキーに使用）
            cache.put_rows(
                "fins_summary",
                api_data,
                key_columns=["Code", "DiscDate"],
            )

        # マージ（date 指定なしの場合、キャッシュと API を統合）
        if not date and cached_data:
            seen_keys: set[str] = set()
            merged: list[dict[str, Any]] = []
            for row in api_data:
                key = f"{row.get('Code')}_{row.get('DiscDate')}_{row.get('DiscNo', '')}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    merged.append(row)
            for row in cached_data:
                key = f"{row.get('Code')}_{row.get('DiscDate')}_{row.get('DiscNo', '')}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    merged.append(row)
            merged.sort(key=lambda r: r.get("DiscDate", ""))
            source = "cache+api"
        else:
            merged = api_data
            source = "api"

        merged, split_adjusted = _apply_split_adjustment(merged, cache)
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

    except (
        APIError,
        InvalidAPIKeyError,
        UserNotConfiguredError,
        DecryptionError,
        UserNotAllowedError,
    ) as e:
        return format_api_error(e)
