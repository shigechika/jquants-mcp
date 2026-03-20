"""Equity-related tools for jquants-dat-mcp."""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP

from ..cache.store import ENDPOINT_TTL, CacheStore, TTL_24H, TTL_90D, make_cache_key
from ..client import JQuantsClient
from ..exceptions import APIError, UserNotConfiguredError, format_api_error

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

        上場銘柄一覧を取得する。銘柄コードや基準日を指定して、会社名・業種・市場区分等を取得できる。
        パラメータ省略時は当日の全銘柄情報を返す。

        [対応プラン] Free / Light / Standard / Premium

        Args:
            code: 銘柄コード（5桁 例: 27800、4桁指定時は普通株式のみ）
            date: 基準日（YYYYMMDD or YYYY-MM-DD）
        """
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
        except (APIError, UserNotConfiguredError) as e:
            return format_api_error(e)

    @mcp.tool()
    async def get_equities_bars_daily(
        code: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve daily stock prices (OHLC).

        株価四本値（日足）を取得する。銘柄コードまたは日付を指定して株価データを取得できる。
        code または date のいずれかは必須。
        調整後株価（AdjO/AdjC等）や前場・後場の内訳も含む。

        [対応プラン] Free / Light / Standard / Premium
        ※ Free プランはデータが12週間遅延

        Args:
            code: 銘柄コード（5桁 例: 27800、4桁指定時は普通株式のみ）
            date: 日付（YYYYMMDD or YYYY-MM-DD）
            date_from: 期間指定の開始日
            date_to: 期間指定の終了日
        """
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
        except (APIError, UserNotConfiguredError) as e:
            return format_api_error(e)

    @mcp.tool()
    async def get_equities_bars_minute(
        code: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve minute-level stock prices (OHLC).

        株価分足データを取得する。1分単位の四本値（OHLC）、出来高、売買代金を取得できる。
        データ取得可能期間は過去2年間。

        [対応プラン] Light / Standard / Premium（株価 分足・ティック アドオン契約が必要）

        Args:
            code: 銘柄コード（5桁 例: 27800、4桁指定時は普通株式のみ）
            date: 日付（YYYYMMDD or YYYY-MM-DD）
            date_from: 期間指定の開始日
            date_to: 期間指定の終了日
        """
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
        except (APIError, UserNotConfiguredError) as e:
            return format_api_error(e)

    @mcp.tool()
    async def get_equities_bars_daily_am(
        code: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve morning session stock prices (OHLC).

        当日の前場四本値（始値・高値・安値・終値）と出来高を取得する。
        当日12:00頃に更新され、翌日6:00頃まで取得可能。
        過去データは get_equities_bars_daily の前場列（MO/MH/ML/MC）で取得できる。

        [対応プラン] Premium

        Args:
            code: 銘柄コード（5桁 例: 27800、4桁指定時は普通株式のみ）。省略時は全銘柄。
        """
        client: JQuantsClient = await get_client()

        # リアルタイムデータのためキャッシュしない
        try:
            data = await client.get_all_pages("/equities/bars/daily/am", {"code": code})
            return {"count": len(data), "data": data}
        except (APIError, UserNotConfiguredError) as e:
            return format_api_error(e)

    @mcp.tool()
    async def get_equities_investor_types(
        section: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve trading data by type of investors.

        投資部門別の売買情報を取得する。自己・委託・海外投資家・個人・信託銀行など
        の投資部門別に売買代金を取得できる。週次（通常木曜日）に更新。

        [対応プラン] Light / Standard / Premium

        Args:
            section: 市場区分（例: TSEPrime, TSEStandard, TSEGrowth）
            date_from: 期間指定の開始日（YYYYMMDD or YYYY-MM-DD）
            date_to: 期間指定の終了日（YYYYMMDD or YYYY-MM-DD）
        """
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
        except (APIError, UserNotConfiguredError) as e:
            return format_api_error(e)

    @mcp.tool()
    async def get_equities_earnings_calendar(
        date: str | None = None,
        code: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve earnings announcement schedule.

        決算発表予定を取得する。日次取得で約3ヶ月分を蓄積。
        日付指定でその日の発表予定、銘柄コード指定で直近の決算日を検索できる。
        対象は3月期・9月期決算企業（REIT除く）。

        [対応プラン] Free / Light / Standard / Premium

        Args:
            date: 発表予定日(YYYYMMDD or YYYY-MM-DD)。省略時は最新データ。
            code: 銘柄コード(5桁 例: 72030、4桁指定時は末尾0を補完)。
                  指定時は蓄積データから該当銘柄の決算予定を検索。
        """
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
        except (APIError, UserNotConfiguredError) as e:
            return format_api_error(e)

    def _search_earnings_by_code(cache: CacheStore, code: str) -> dict[str, Any]:
        """Search accumulated earnings calendar data for a specific stock code."""
        import json

        conn = cache._ensure_connection()
        # cache_key format: "/equities/earnings-calendar|date=YYYYMMDD|plan=<plan>"
        plan = cache.default_plan
        rows = conn.execute(
            "SELECT data FROM response_cache "
            "WHERE cache_key LIKE ? AND cache_key LIKE ?",
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

    except (APIError, UserNotConfiguredError) as e:
        return format_api_error(e)
