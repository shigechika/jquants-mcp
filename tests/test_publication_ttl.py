"""Tier 2 entries expire at the next daily publication, not 24h after fetch (#632)."""

from __future__ import annotations

import json
import time
from datetime import datetime

import pytest

from jquants_mcp.cache.publication import (
    JST,
    LATE_RECHECK_SECONDS,
    LATE_RECHECK_SLOW_SECONDS,
    newest_date,
    ttl_until_next_publication,
)
from jquants_mcp.cache.store import TTL_24H, calendar_is_business_day

SHORT_RATIO = "/markets/short-ratio"  # around 16:30 JST, rows dated the trading day
TODAY_ROWS = [{"Date": "2026-10-12"}, {"Date": "2026-10-13"}]
PREVIOUS_ROWS = [{"Date": "2026-10-12"}]


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    # 2026-10-13 is a Tuesday; 10-17 is a Saturday.
    return datetime(2026, 10, day, hour, minute, tzinfo=JST)


def _ttl(now, rows=None, endpoint=SHORT_RATIO, business_day=None):
    return ttl_until_next_publication(endpoint, rows, now=now, business_day=business_day)


def test_before_publication_expires_at_todays_publication():
    # The issue's case: a morning fetch must not be served until the next evening.
    assert _ttl(_at(13, 10), PREVIOUS_ROWS) == 6 * 3600 + 30 * 60


def test_delivered_today_waits_for_the_next_publication():
    # No re-checks once the response shows today's delivery (review: wasted refetches).
    assert _ttl(_at(13, 16, 45), TODAY_ROWS) == 23 * 3600 + 45 * 60


def test_not_yet_delivered_rechecks_every_15_minutes_in_the_window():
    assert _ttl(_at(13, 16, 45), PREVIOUS_ROWS) == LATE_RECHECK_SECONDS


def test_very_late_delivery_keeps_rechecking_hourly():
    # Review: a delivery later than the two-hour window must not bring back 22h staleness.
    assert _ttl(_at(13, 19), PREVIOUS_ROWS) == LATE_RECHECK_SLOW_SECONDS


def test_unreadable_answer_falls_back_to_the_clock():
    assert _ttl(_at(13, 16, 45), []) == LATE_RECHECK_SECONDS
    assert _ttl(_at(13, 19), []) == 21 * 3600 + 30 * 60


def test_weekend_and_exchange_holiday_do_not_recheck():
    assert _ttl(_at(17, 16, 45), PREVIOUS_ROWS) == 23 * 3600 + 45 * 60
    # A weekday exchange holiday, known from the calendar.
    assert _ttl(_at(13, 16, 45), PREVIOUS_ROWS, business_day=False) == 23 * 3600 + 45 * 60


def test_utc_and_naive_clocks_are_read_as_jst():
    utc = datetime.fromisoformat("2026-10-13T01:00:00+00:00")  # 10:00 JST, as on Cloud Run
    assert _ttl(utc) == 6 * 3600 + 30 * 60
    assert _ttl(datetime(2026, 10, 13, 10, 0)) == 6 * 3600 + 30 * 60


def test_margin_interest_reads_its_publication_date():
    # Rows are dated the previous business day; PubDate is the delivery day.
    rows = [{"Date": "2026-10-12", "PubDate": "2026-10-13"}]
    assert _ttl(_at(13, 16, 15), rows, "/markets/margin-interest") == 23 * 3600 + 45 * 60
    stale = [{"Date": "2026-10-09", "PubDate": "2026-10-12"}]
    assert _ttl(_at(13, 16, 15), stale, "/markets/margin-interest") == LATE_RECHECK_SECONDS


def test_short_sale_report_relies_on_the_clock_alone():
    # Per-code answers can hold a weeks-old newest report, so rows are ignored.
    rows = [{"DiscDate": "2026-10-13"}]
    assert _ttl(_at(13, 18), rows, "/markets/short-sale-report") == LATE_RECHECK_SECONDS


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
    assert _ttl(_at(13, 12), None, endpoint) == (hour - 12) * 3600 + minute * 60


def test_unknown_endpoint_keeps_24h():
    assert _ttl(_at(13, 12), None, "/equities/bars/daily") == TTL_24H


def test_newest_date_ignores_bad_values():
    rows = [
        {"Date": "2026-10-13"},
        {"Date": ""},
        {"Date": None},
        {"x": 1},
        "junk",
        {"Date": "20261014"},
    ]
    assert newest_date(rows, "Date") == "2026-10-13"


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"HolDiv": "1"}, True),
        ({"HolDiv": "2"}, True),
        ({"HolDiv": "0"}, False),
        ({"HolDiv": "3"}, False),
        ({"HolidayDivision": "1"}, True),
        ({"Date": "2026-10-13"}, None),
    ],
)
def test_calendar_is_business_day(row, expected):
    assert calendar_is_business_day(row) is expected


# --- wiring -----------------------------------------------------------------


class _Cache:
    def __init__(self, business_day=None):
        self.ttl = None
        self.business_day = business_day

    def get_response(self, key):
        return None

    def put_response(self, key, value, ttl_seconds):
        self.ttl = ttl_seconds

    def is_business_day(self, day):
        return self.business_day


class _Client:
    async def get_all_pages(self, endpoint, params):
        return [{"Date": "2026-10-13"}]


async def test_tier2_fallback_passes_rows_and_business_day(monkeypatch):
    from jquants_mcp.tools import markets

    seen = {}

    def fake(endpoint, rows, *, business_day):
        seen.update(endpoint=endpoint, rows=rows, business_day=business_day)
        return 123

    monkeypatch.setattr(markets, "ttl_until_next_publication", fake)
    cache = _Cache(business_day=False)
    result = await markets._tier2_fallback(_Client(), cache, SHORT_RATIO, {})
    assert result["count"] == 1 and cache.ttl == 123
    assert seen == {
        "endpoint": SHORT_RATIO,
        "rows": [{"Date": "2026-10-13"}],
        "business_day": False,
    }


@pytest.mark.parametrize(
    ("params", "historical"),
    [
        ({"code": "72030"}, False),
        ({"disc_date": "2020-01-06"}, True),
        ({"disc_date_from": "20200101", "disc_date_to": "20200131"}, True),
        ({"disc_date_from": "20200101"}, False),
        ({"calc_date": "2020-01-06"}, False),  # disclosed business days later
        ({"disc_date": "2999-01-01"}, False),
    ],
)
def test_short_sale_historical_queries(params, historical):
    from jquants_mcp.tools.markets import _short_sale_query_is_historical

    assert _short_sale_query_is_historical(params) is historical


def test_store_reads_business_day_from_calendar(tmp_path):
    from jquants_mcp.cache.store import CacheStore

    store = CacheStore(db_path=tmp_path / "c.db")
    conn = store._ensure_connection()
    for d, hol in (("2026-10-12", "3"), ("2026-10-13", "1")):
        conn.execute(
            "INSERT INTO markets_calendar (date, data, fetched_at) VALUES (?,?,?)",
            (d, json.dumps({"Date": d, "HolDiv": hol}), time.time()),
        )
    conn.commit()
    assert store.is_business_day("2026-10-13") is True
    assert store.is_business_day("2026-10-12") is False
    assert store.is_business_day("2026-10-14") is None


async def _call_short_sale(monkeypatch, **args):
    from mcp.server.mcpserver import MCPServer

    from jquants_mcp.tools import markets

    seen = {}
    monkeypatch.setattr(
        markets,
        "ttl_until_next_publication",
        lambda endpoint, rows, *, business_day: seen.setdefault("endpoint", endpoint) and 77,
    )
    server = MCPServer("t")
    cache = _Cache()

    async def get_client():
        return _Client()

    markets.register(server, get_client, lambda: cache)
    await server.call_tool("get_markets_short_sale_report", args)
    return cache.ttl, seen


async def test_short_sale_report_uses_the_publication_ttl(monkeypatch):
    ttl, seen = await _call_short_sale(monkeypatch, code="72030")
    assert ttl == 77 and seen == {"endpoint": "/markets/short-sale-report"}


async def test_historical_short_sale_report_keeps_24h(monkeypatch):
    ttl, seen = await _call_short_sale(monkeypatch, code="72030", disc_date="2020-01-06")
    assert ttl == TTL_24H and seen == {}


def test_trading_date_today_skips_non_business_rows(tmp_path):
    # Same HolDivision == "0" bug as _latest_trading_day (#632 review).
    from datetime import date, timedelta

    from jquants_mcp.cache.store import CacheStore

    store = CacheStore(db_path=tmp_path / "c.db")
    conn = store._ensure_connection()
    today = date.today()
    rows = [(today - timedelta(days=2), "1"), (today - timedelta(days=1), "2"), (today, "0")]
    for d, hol in rows:
        conn.execute(
            "INSERT INTO markets_calendar (date, data, fetched_at) VALUES (?,?,?)",
            (d.isoformat(), json.dumps({"HolDiv": hol}), time.time()),
        )
    conn.commit()
    assert store.get_trading_date_today() == (today - timedelta(days=1)).isoformat()
