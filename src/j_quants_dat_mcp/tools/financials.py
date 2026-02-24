"""Financial data tools for j-quants-dat-mcp."""

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
    """Register financial tools on the MCP server."""

    @mcp.tool()
    async def get_fins_summary(
        code: str | None = None,
        date: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve financial summary data.

        財務情報（四半期決算）を取得する。売上高・営業利益・当期純利益・EPS・BPS・
        キャッシュフロー・配当金・業績予想など包括的な財務データを含む。
        code または date のいずれかは必須。

        [対応プラン] Free / Light / Standard / Premium
        ※ Free プランはデータが12週間遅延

        Args:
            code: 銘柄コード（5桁 例: 27800、4桁指定時は普通株式のみ）
            date: 日付（YYYYMMDD or YYYY-MM-DD）。指定日に開示された財務情報を取得。
        """
        client: JQuantsClient = get_client()
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
        except APIError as e:
            return format_api_error(e)

    @mcp.tool()
    async def get_fins_details(
        code: str | None = None,
        date: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve detailed financial statements (BS/PL/CF).

        財務諸表の詳細データを取得する。貸借対照表（BS）・損益計算書（PL）の
        各項目を日本基準・IFRS に対応して取得できる。
        code または date のいずれかは必須。

        [対応プラン] Premium

        Args:
            code: 銘柄コード（5桁 例: 27800、4桁指定時は普通株式のみ）
            date: 日付（YYYY-MM-DD）。指定日に開示された財務諸表を取得。
        """
        client: JQuantsClient = get_client()
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
        except APIError as e:
            return format_api_error(e)

    @mcp.tool()
    async def get_fins_dividend(
        code: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve cash dividend data.

        配当金データを取得する。権利確定日・配当落ち日・配当金額（予想・実績）・
        配当支払開始予定日・記念特別配当情報などを含む。

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
        cache_key = make_cache_key("/fins/dividend", params)
        cached = cache.get_response(cache_key)
        if cached is not None:
            return cached

        try:
            data = await client.get_all_pages("/fins/dividend", params)
            result = {"count": len(data), "data": data}
            cache.put_response(cache_key, result, ttl_seconds=TTL_24H)
            return result
        except APIError as e:
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

    except APIError as e:
        return format_api_error(e)
