"""Market-related tools for jquants-dat-mcp."""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP

from ..cache.store import CacheStore, TTL_24H, make_cache_key
from ..client import JQuantsClient
from ..exceptions import APIError, format_api_error

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

        # code 指定 + 期間指定の場合は Tier 1 キャッシュで増分取得
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
        # short_sale_report は Tier 2 のまま（同一銘柄+日付に複数報告者のレコードあり）
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
    """Markets ツール用の汎用 Tier 1 キャッシュ取得。

    Args:
        date_field: API レスポンスの日付フィールド名（例: "Date", "PubDate"）
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

        # API パラメータの構築（ハイフン除去）
        params: dict[str, Any] = {}
        if key_value:
            params[key_name] = key_value
        if date:
            params["date"] = _normalize_date(date)
        if date_from:
            params["from"] = _normalize_date(date_from)
        if date_to:
            params["to"] = _normalize_date(date_to)

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

    except APIError as e:
        return format_api_error(e)


async def _tier2_fallback(
    client: JQuantsClient,
    cache: CacheStore,
    endpoint: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """パラメータなし呼び出し時の Tier 2 フォールバック。"""
    cache_key = make_cache_key(endpoint, params)
    cached = cache.get_response(cache_key)
    if cached is not None:
        return cached

    try:
        data = await client.get_all_pages(endpoint, params)
        result = {"count": len(data), "data": data}
        cache.put_response(cache_key, result, ttl_seconds=TTL_24H)
        return result
    except APIError as e:
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
    """取引カレンダーを Tier 1 キャッシュ付きで取得する。"""
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

        # API パラメータ（ハイフン除去）
        params: dict[str, Any] = {}
        if hol_div:
            params["hol_div"] = hol_div
        if date_from:
            params["from"] = _normalize_date(date_from)
        if date_to:
            params["to"] = _normalize_date(date_to)

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

    except APIError as e:
        return format_api_error(e)


def _filter_hol_div(data: list[dict[str, Any]], hol_div: str | None) -> list[dict[str, Any]]:
    """hol_div でフィルタ（キャッシュには全件保存するため後フィルタ）。"""
    if hol_div is None:
        return data
    return [r for r in data if str(r.get("HolDiv", "")) == hol_div]
