"""Tier 2 entries expire at the next daily publication, not 24h after fetch (#632)."""

from __future__ import annotations

from datetime import datetime

import pytest

from jquants_mcp.cache.publication import (
    JST,
    LATE_RECHECK_SECONDS,
    ttl_until_next_publication,
)
from jquants_mcp.cache.store import TTL_24H

SHORT_RATIO = "/markets/short-ratio"  # published around 16:30 JST


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    # 2026-10-13 is a Tuesday; 10-17 is a Saturday.
    return datetime(2026, 10, day, hour, minute, tzinfo=JST)


def test_before_publication_expires_at_todays_publication():
    # The issue's case: a morning fetch must not be served until the next evening.
    assert ttl_until_next_publication(SHORT_RATIO, _at(13, 10)) == 6 * 3600 + 30 * 60


def test_just_after_publication_rechecks_for_a_late_delivery():
    assert ttl_until_next_publication(SHORT_RATIO, _at(13, 16, 45)) == LATE_RECHECK_SECONDS


def test_after_the_late_window_waits_for_the_next_publication():
    # 19:00 -> next day 16:30.
    assert ttl_until_next_publication(SHORT_RATIO, _at(13, 19)) == 21 * 3600 + 30 * 60


def test_weekend_skips_the_late_window():
    assert ttl_until_next_publication(SHORT_RATIO, _at(17, 16, 45)) == 23 * 3600 + 45 * 60


def test_utc_clock_is_converted_to_jst():
    # 01:00 UTC is 10:00 JST, as on Cloud Run.
    utc = datetime.fromisoformat("2026-10-13T01:00:00+00:00")
    assert ttl_until_next_publication(SHORT_RATIO, utc) == 6 * 3600 + 30 * 60


@pytest.mark.parametrize(
    ("endpoint", "hour", "minute"),
    [
        ("/markets/margin-interest", 16, 0),
        ("/markets/margin-alert", 16, 30),
        ("/markets/short-sale-report", 17, 30),
        ("/markets/breakdown", 18, 0),
    ],
)
def test_each_endpoint_uses_its_own_publication_time(endpoint, hour, minute):
    expected = (hour - 12) * 3600 + minute * 60
    assert ttl_until_next_publication(endpoint, _at(13, 12)) == expected


def test_unknown_endpoint_keeps_24h():
    assert ttl_until_next_publication("/equities/bars/daily", _at(13, 12)) == TTL_24H


async def test_tier2_fallback_caches_until_publication(monkeypatch):
    from jquants_mcp.tools import markets

    stored = {}

    class Cache:
        def get_response(self, key):
            return None

        def put_response(self, key, value, ttl_seconds):
            stored["ttl"] = ttl_seconds

    class Client:
        async def get_all_pages(self, endpoint, params):
            return [{"Date": "2026-10-12"}]

    monkeypatch.setattr(
        markets, "ttl_until_next_publication", lambda ep: 123 if ep == SHORT_RATIO else -1
    )
    result = await markets._tier2_fallback(Client(), Cache(), SHORT_RATIO, {})
    assert result["count"] == 1 and stored["ttl"] == 123
