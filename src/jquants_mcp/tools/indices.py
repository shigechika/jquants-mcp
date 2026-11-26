"""Index-related tools for jquants-mcp."""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver import MCPServer

from ..cache.store import CacheStore, TTL_24H, make_cache_key
from ..client import JQuantsClient
from ..exceptions import (
    TOOL_API_ERRORS,
    format_api_error,
)
from ..tool_annotations import READ_ONLY_API
from .equities import _get_code_series_with_cache
from ..validators import (
    collect_errors,
    make_validation_error_response,
    validate_code,
    validate_date,
)

logger = logging.getLogger(__name__)


def register(
    mcp: MCPServer,
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
        except TOOL_API_ERRORS as e:
            return format_api_error(e)

    @mcp.tool(annotations=READ_ONLY_API)
    async def get_indices_bars_daily_topix(
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve daily TOPIX bars (OHLC) with Tier 1 cache.

        Returns daily OHLC data for TOPIX using a dedicated endpoint.
        Supports efficient incremental fetching via row-level (Tier 1) cache.
        partial=true: rows may be missing
        (the API failed and cached rows were returned, or the page limit
        cut the answer short).

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
    """Retrieve TOPIX daily bars with Tier 1 cache.

    Uses the same completeness proof as the per-stock series
    (``_get_code_series_with_cache``, jquants-mcp#625): the cache alone answers
    only when every expected trading day of the range is cached; a missing tail
    is fetched incrementally and any other gap refetches the range. The previous
    "latest cached date >= date_to" check served a range with a hole in it.
    """
    return await _get_code_series_with_cache(
        client,
        cache,
        table="indices_bars_daily_topix",
        endpoint="/indices/bars/daily/topix",
        head_name="topix_head",
        code=None,
        date=None,
        date_from=date_from,
        date_to=date_to,
    )
