"""Index-related tools for jquants-mcp."""

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


def register(
    mcp: FastMCP,
    get_client: callable,
    get_cache: callable,
) -> None:
    """Register index tools on the MCP server."""

    @mcp.tool(annotations=READ_ONLY_API)
    async def get_indices_bars_daily(
        code: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve daily index bars (OHLC).

        Returns daily OHLC and volume for indices including TOPIX, Nikkei 225, and Growth 250.

        [Supported plans] Standard / Premium

        Args:
            code: Index code (e.g. 0000 = TOPIX, 0010 = Nikkei 225)
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
        cache_key = make_cache_key("/indices/bars/daily", params)
        cached = cache.get_response(cache_key)
        if cached is not None:
            return cached

        try:
            data = await client.get_all_pages("/indices/bars/daily", params)
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
    async def get_indices_bars_daily_topix(
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve daily TOPIX bars (OHLC) with Tier 1 cache.

        Returns daily OHLC data for TOPIX using a dedicated endpoint.
        Supports efficient incremental fetching via row-level (Tier 1) cache.

        [Supported plans] Light / Standard / Premium

        Args:
            date_from: Start date for range query (YYYYMMDD or YYYY-MM-DD)
            date_to: End date for range query (YYYYMMDD or YYYY-MM-DD)
        """
        errors = collect_errors(
            validate_date(date_from, "date_from"),
            validate_date(date_to, "date_to"),
        )
        if errors:
            return make_validation_error_response(errors)

        client: JQuantsClient = await get_client()
        cache: CacheStore = get_cache()

        return await _get_topix_with_cache(client, cache, date_from, date_to)


async def _get_topix_with_cache(
    client: JQuantsClient,
    cache: CacheStore,
    date_from: str | None,
    date_to: str | None,
) -> dict[str, Any]:
    """Retrieve TOPIX daily bars with Tier 1 cache."""
    try:
        # Fetch existing data from the cache
        cached_data = cache.get_rows(
            "indices_bars_daily_topix",
            key_filter={},
            date_from=date_from,
            date_to=date_to,
        )

        # Check which dates are already cached
        cached_dates = cache.get_cached_dates(
            "indices_bars_daily_topix",
            key_filter={},
            date_from=date_from,
            date_to=date_to,
        )

        params: dict[str, Any] = {}
        if date_from:
            params["from"] = date_from
        if date_to:
            params["to"] = date_to

        # Filter out malformed keys ("None", "", timestamps with spaces/T-separator)
        # that would corrupt params["from"] after client-side hyphen stripping.
        # Only accept "YYYY-MM-DD" (10) or "YYYYMMDD" (8) — the two formats the API accepts.
        valid_dates = {d for d in cached_dates if d and d[0].isdigit() and len(d) in (8, 10)}
        if valid_dates:
            # Incremental fetch: retrieve everything after the latest cached date
            latest_cached = max(valid_dates)
            if date_to and latest_cached >= date_to:
                # Entire range already cached
                logger.info("TOPIX fully cached (%d rows)", len(cached_data))
                return {"count": len(cached_data), "data": cached_data, "source": "cache"}
            params["from"] = latest_cached

        api_data = await client.get_all_pages("/indices/bars/daily/topix", params)

        if api_data:
            cache.put_rows(
                "indices_bars_daily_topix",
                api_data,
                key_columns=["Date"],
            )

        # Merge (dedup)
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
