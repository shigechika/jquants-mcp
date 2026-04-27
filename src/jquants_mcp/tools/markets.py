"""Market-related tools for jquants-dat-mcp."""

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
    validate_sector33,
)

logger = logging.getLogger(__name__)


def _normalize_date(d: str | None) -> str | None:
    """Normalize date string to YYYYMMDD format (strip hyphens)."""
    if d is None:
        return None
    return d.replace("-", "")


def register(
    mcp: FastMCP,
    get_client: callable,
    get_cache: callable,
) -> None:
    """Register market tools on the MCP server."""

    @mcp.tool(annotations=READ_ONLY_API)
    async def get_markets_margin_interest(
        code: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Per-stock margin balance: 買残・売残・貸借倍率 (信用残). Standard+ only.

        Use for 信用残, 買い残, 売り残, 貸借倍率, margin loan/short balance.
        For margin trading restrictions (追証・増担保規制), use get_markets_margin_alert instead.

        [Supported plans] Standard / Premium

        Args:
            code: Stock code (5 digits, e.g. 27800)
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

        # いずれかのパラメータ指定ありの場合は Tier 1 キャッシュで増分取得
        if code or date or date_from or date_to:
            return await _get_with_tier1_cache(
                client,
                cache,
                table="markets_margin_interest",
                endpoint="/markets/margin-interest",
                key_name="code",
                key_value=code,
                key_column="Code",
                date=date,
                date_from=date_from,
                date_to=date_to,
            )

        # パラメータなし: Tier 2 フォールバック
        return await _tier2_fallback(client, cache, "/markets/margin-interest", {})

    @mcp.tool(annotations=READ_ONLY_API)
    async def get_markets_margin_alert(
        code: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Per-stock margin trading restriction status: 増担保規制・信用規制. Standard+ only.

        Use for 追証, 規制銘柄, 増担保規制, 信用規制, margin restriction/alert.
        For margin balances (買残・売残・貸借倍率), use get_markets_margin_interest instead.

        [Supported plans] Standard / Premium

        Args:
            code: Stock code (5 digits, e.g. 27800)
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

        if code or date or date_from or date_to:
            return await _get_with_tier1_cache(
                client,
                cache,
                table="markets_margin_alert",
                endpoint="/markets/margin-alert",
                key_name="code",
                key_value=code,
                key_column="Code",
                date=date,
                date_from=date_from,
                date_to=date_to,
                date_field="PubDate",
            )

        return await _tier2_fallback(client, cache, "/markets/margin-alert", {})

    @mcp.tool(annotations=READ_ONLY_API)
    async def get_markets_short_ratio(
        s33: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """TSE 33-sector short selling ratio (業種別空売り比率). Standard+ only.

        Use for 業種別空売り比率, sector-level 空売り動向, industry short selling trends.
        Keyed by s33 sector code — not per stock. For per-stock institutional short
        positions (大量空売り残高), use get_markets_short_sale_report instead.

        [Supported plans] Standard / Premium

        Args:
            s33: TSE 33-sector code (e.g. 0050 = Fishery, Agriculture & Forestry)
            date: Date (YYYYMMDD or YYYY-MM-DD)
            date_from: Start date for range query
            date_to: End date for range query
        """
        errors = collect_errors(
            validate_sector33(s33, "s33"),
            validate_date(date),
            validate_date(date_from, "date_from"),
            validate_date(date_to, "date_to"),
        )
        if errors:
            return make_validation_error_response(errors)

        client: JQuantsClient = await get_client()
        cache: CacheStore = get_cache()

        if s33 or date or date_from or date_to:
            return await _get_with_tier1_cache(
                client,
                cache,
                table="markets_short_ratio",
                endpoint="/markets/short-ratio",
                key_name="s33",
                key_value=s33,
                key_column="S33",
                date=date,
                date_from=date_from,
                date_to=date_to,
            )

        return await _tier2_fallback(client, cache, "/markets/short-ratio", {})

    @mcp.tool(annotations=READ_ONLY_API)
    async def get_markets_short_sale_report(
        code: str | None = None,
        disc_date: str | None = None,
        disc_date_from: str | None = None,
        disc_date_to: str | None = None,
        calc_date: str | None = None,
    ) -> dict[str, Any]:
        """Per-stock institutional short sale positions (大量空売り残高). Standard+ only.

        Use for 大量空売り残高, 空売り残, institutional short positions, short squeeze screening.
        Positions disclosed weekly per institution.
        Pair with get_equities_earnings_calendar for 決算またぎ空売り残 / 踏み上げリスク screening.
        For sector-level short ratios (業種別空売り比率), use get_markets_short_ratio instead.

        [Supported plans] Standard / Premium

        Args:
            code: Stock code (5 digits, e.g. 27800)
            disc_date: Disclosure date (YYYYMMDD or YYYY-MM-DD)
            disc_date_from: Start disclosure date for range query
            disc_date_to: End disclosure date for range query
            calc_date: Calculation date (YYYYMMDD or YYYY-MM-DD)
        """
        # short_sale_report は Tier 2 のまま（同一銘柄+日付に複数報告者のレコードあり）
        client: JQuantsClient = await get_client()
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
        except (
            APIError,
            InvalidAPIKeyError,
            UserNotConfiguredError,
            DecryptionError,
            UserNotAllowedError,
        ) as e:
            return format_api_error(e)

    @mcp.tool(annotations=READ_ONLY_API)
    async def get_markets_breakdown(
        code: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve market breakdown data (sell/buy by investor type per issue).

        Returns daily buy/sell breakdown by investor type (proprietary, brokered, foreign, etc.)
        per individual issue.

        [Supported plans] Premium

        Args:
            code: Stock code (5 digits, e.g. 27800; 4-digit codes match ordinary shares only)
            date: Date (YYYYMMDD or YYYY-MM-DD)
            date_from: Start date for range query
            date_to: End date for range query
        """
        client: JQuantsClient = await get_client()
        cache: CacheStore = get_cache()

        if code or date or date_from or date_to:
            return await _get_with_tier1_cache(
                client,
                cache,
                table="markets_breakdown",
                endpoint="/markets/breakdown",
                key_name="code",
                key_value=code,
                key_column="Code",
                date=date,
                date_from=date_from,
                date_to=date_to,
            )

        return await _tier2_fallback(client, cache, "/markets/breakdown", {})

    @mcp.tool(annotations=READ_ONLY_API)
    async def get_markets_calendar(
        hol_div: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve market calendar (trading days and holidays).

        Returns trading calendar data including trading days, holidays, and half-day classifications.
        All data is returned in a single response without pagination.

        [Supported plans] Free / Light / Standard / Premium

        Args:
            hol_div: Holiday type filter (e.g. 0 = trading day, 1 = holiday, 2 = special holiday)
            date_from: Start date for range query (YYYYMMDD or YYYY-MM-DD)
            date_to: End date for range query (YYYYMMDD or YYYY-MM-DD)
        """
        client: JQuantsClient = await get_client()
        cache: CacheStore = get_cache()

        return await _get_calendar_with_cache(
            client,
            cache,
            hol_div,
            date_from,
            date_to,
        )


# ------------------------------------------------------------------
# Tier 1 キャッシュヘルパー: code+date / s33+date パターン
# ------------------------------------------------------------------


async def _get_with_tier1_cache(
    client: JQuantsClient,
    cache: CacheStore,
    *,
    table: str,
    endpoint: str,
    key_name: str,
    key_value: str | None,
    key_column: str,
    date: str | None,
    date_from: str | None,
    date_to: str | None,
    date_field: str = "Date",
) -> dict[str, Any]:
    """Generic Tier 1 cache retrieval for market tools.

    Args:
        date_field: Date field name in the API response (e.g. "Date", "PubDate").
    """
    try:
        # キーフィルタの構築
        key_filter: dict[str, str] = {}
        if key_value:
            cache_key_val = (
                key_value + "0" if len(key_value) == 4 and key_name == "code" else key_value
            )
            key_filter[key_name] = cache_key_val

        effective_date_from = date or date_from
        # キャッシュから既存データを取得
        cached_data = cache.get_rows(
            table,
            key_filter=key_filter,
            date_from=effective_date_from,
            date_to=date_to,
        )

        # API パラメータの構築
        params: dict[str, Any] = {}
        if key_value:
            params[key_name] = key_value
        if date:
            params["date"] = date
        if date_from:
            params["from"] = date_from
        if date_to:
            params["to"] = date_to

        # キャッシュ済み日付の確認
        cached_dates = cache.get_cached_dates(
            table,
            key_filter=key_filter,
            date_from=effective_date_from,
            date_to=date_to,
        )

        if cached_dates and not date:
            # 増分取得: キャッシュの最新日付以降を取得
            latest_cached = max(cached_dates)
            if date_to and latest_cached >= date_to:
                logger.info("%s 全データキャッシュ済み (%d件)", table, len(cached_data))
                return {"count": len(cached_data), "data": cached_data, "source": "cache"}
            params["from"] = latest_cached

        try:
            api_data = await client.get_all_pages(endpoint, params)
        except APIError:
            if cached_data:
                logger.info("API失敗、キャッシュデータを返却: %s (%d件)", table, len(cached_data))
                return {"count": len(cached_data), "data": cached_data, "source": "cache"}
            raise

        if api_data:
            cache.put_rows(
                table,
                api_data,
                key_columns=[key_column, date_field],
            )

        # マージ（重複排除）: API データ優先
        seen_keys: set[str] = set()
        merged: list[dict[str, Any]] = []
        for row in api_data:
            key = f"{row.get(key_column, '')}_{row.get(date_field, '')}"
            if key not in seen_keys:
                seen_keys.add(key)
                merged.append(row)
        for row in cached_data:
            key = f"{row.get(key_column, '')}_{row.get(date_field, '')}"
            if key not in seen_keys:
                seen_keys.add(key)
                merged.append(row)

        merged.sort(key=lambda r: r.get(date_field, ""))

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


async def _tier2_fallback(
    client: JQuantsClient,
    cache: CacheStore,
    endpoint: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Tier 2 fallback for calls without parameters."""
    cache_key = make_cache_key(endpoint, params)
    cached = cache.get_response(cache_key)
    if cached is not None:
        return cached

    try:
        data = await client.get_all_pages(endpoint, params)
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
# Tier 1 キャッシュ: カレンダー（date のみ、Pattern C）
# ------------------------------------------------------------------


async def _get_calendar_with_cache(
    client: JQuantsClient,
    cache: CacheStore,
    hol_div: str | None,
    date_from: str | None,
    date_to: str | None,
) -> dict[str, Any]:
    """Retrieve market calendar with Tier 1 cache."""
    try:
        # キャッシュから既存データを取得
        cached_data = cache.get_rows(
            "markets_calendar",
            key_filter={},
            date_from=date_from,
            date_to=date_to,
        )

        # キャッシュ済み日付の確認
        cached_dates = cache.get_cached_dates(
            "markets_calendar",
            key_filter={},
            date_from=date_from,
            date_to=date_to,
        )

        params: dict[str, Any] = {}
        if hol_div:
            params["hol_div"] = hol_div
        if date_from:
            params["from"] = date_from
        if date_to:
            params["to"] = date_to

        if cached_dates:
            latest_cached = max(cached_dates)
            if date_to and latest_cached >= date_to:
                logger.info("カレンダー全データキャッシュ済み (%d件)", len(cached_data))
                # hol_div フィルタ適用
                filtered = _filter_hol_div(cached_data, hol_div)
                return {"count": len(filtered), "data": filtered, "source": "cache"}
            params["from"] = latest_cached

        # カレンダーはページネーションなし
        response = await client.get("/markets/calendar", params)
        api_data = response.get("data", [])

        if api_data:
            cache.put_rows(
                "markets_calendar",
                api_data,
                key_columns=["Date"],
            )

        # マージ（重複排除）
        seen_keys: set[str] = set()
        merged: list[dict[str, Any]] = []
        for row in api_data:
            key = row.get("Date", "")
            if key not in seen_keys:
                seen_keys.add(key)
                merged.append(row)
        for row in cached_data:
            key = row.get("Date", "")
            if key not in seen_keys:
                seen_keys.add(key)
                merged.append(row)

        merged.sort(key=lambda r: r.get("Date", ""))

        # hol_div フィルタ適用
        filtered = _filter_hol_div(merged, hol_div)

        source = "cache+api" if cached_data and api_data else ("cache" if cached_data else "api")
        return {"count": len(filtered), "data": filtered, "source": source}

    except (
        APIError,
        InvalidAPIKeyError,
        UserNotConfiguredError,
        DecryptionError,
        UserNotAllowedError,
    ) as e:
        return format_api_error(e)


def _filter_hol_div(data: list[dict[str, Any]], hol_div: str | None) -> list[dict[str, Any]]:
    """Filter by hol_div (post-filter since cache stores all records)."""
    if hol_div is None:
        return data
    return [r for r in data if str(r.get("HolDiv", "")) == hol_div]
