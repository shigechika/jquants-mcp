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
    UserNotConfiguredError,
    format_api_error,
)
from ..validators import (
    collect_errors,
    make_validation_error_response,
    validate_code,
    validate_date,
)

logger = logging.getLogger(__name__)


def register(
    mcp: FastMCP,
    get_client: callable,
    get_cache: callable,
) -> None:
    """Register financial tools on the MCP server."""

    @mcp.tool()
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
            result = {"count": len(data), "data": data}
            cache.put_response(cache_key, result, ttl_seconds=TTL_24H)
            return result
        except (APIError, InvalidAPIKeyError, UserNotConfiguredError, DecryptionError) as e:
            return format_api_error(e)

    @mcp.tool()
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
        except (APIError, InvalidAPIKeyError, UserNotConfiguredError, DecryptionError) as e:
            return format_api_error(e)

    @mcp.tool()
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
        except (APIError, InvalidAPIKeyError, UserNotConfiguredError, DecryptionError) as e:
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
    """財務情報を Tier 1 キャッシュ付きで取得する。"""
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
            # 特定日付のキャッシュがある場合はそのまま返す
            return {"count": len(cached_data), "data": cached_data, "source": "cache"}

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

        return {"count": len(merged), "data": merged, "source": source}

    except (APIError, InvalidAPIKeyError, UserNotConfiguredError, DecryptionError) as e:
        return format_api_error(e)
