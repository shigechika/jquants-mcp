"""Expire Tier 2 response-cache entries at the next upstream publication.

A fixed 24-hour TTL keeps a snapshot fetched just before the daily publication
for almost a full day after newer data is out (#632). These endpoints publish
once per business day around a fixed time, per the J-Quants data update
schedule (https://jpx-jquants.com/en/spec/data-update), so an entry is kept
until the next such time instead.

The schedule says "around", so the clock alone cannot tell whether today's
delivery is out. The response itself can: once the newest publication-dated
row is today's, the delivery has arrived and the entry is kept until the next
publication; until then it is re-checked.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .store import TTL_24H

JST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class Publication:
    """When an endpoint publishes, and which row field carries today's date once it has."""

    hour: int
    minute: int
    #: Field whose newest value equals the publication day once the day's
    #: delivery is out, in a market-wide (parameterless) answer.
    #: margin-interest rows are dated the previous business day (``Date``), so
    #: its publication date ``PubDate`` is used instead. None when the answer
    #: cannot show it: short-sale-report is cached per query (often one code,
    #: whose newest report may be weeks old), so it relies on the clock alone.
    date_field: str | None


PUBLICATIONS: dict[str, Publication] = {
    "/markets/margin-interest": Publication(16, 0, "PubDate"),
    "/markets/margin-alert": Publication(16, 30, "PubDate"),
    "/markets/short-ratio": Publication(16, 30, "Date"),
    "/markets/short-sale-report": Publication(17, 30, None),
    "/markets/breakdown": Publication(18, 0, "Date"),
}

#: After the nominal publication time, an undelivered day is re-checked this
#: often for LATE_WINDOW, then at LATE_RECHECK_SLOW_SECONDS until the next
#: publication, so even a very late delivery is picked up within the hour.
LATE_WINDOW = timedelta(hours=2)
LATE_RECHECK_SECONDS = 15 * 60
LATE_RECHECK_SLOW_SECONDS = 60 * 60

_MIN_TTL_SECONDS = 60


def newest_date(rows: Iterable[Any], field: str) -> str | None:
    """Newest ``YYYY-MM-DD`` value of ``field`` across ``rows``, or None if none parse."""
    newest = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = str(row.get(field) or "")[:10]
        if len(value) == 10 and value[4] == "-" and value[7] == "-":
            newest = value if newest is None or value > newest else newest
    return newest


def ttl_until_next_publication(
    endpoint: str,
    rows: Iterable[Any] | None = None,
    *,
    now: datetime | None = None,
    business_day: bool | None = None,
    previous_business_day: str | None = None,
) -> int:
    """Seconds to cache a response from ``endpoint`` holding ``rows``, fetched at ``now``.

    - Endpoints without a known publication time keep the 24-hour TTL.
    - Before today's publication time, or on a non-business day: until the
      next publication time, unless ``rows`` show that the previous business
      day's delivery is still missing (it ran past midnight or into a
      weekend); then it keeps being re-checked hourly.
    - After it on a business day: until the next publication once ``rows``
      show today's delivery; otherwise re-check (every 15 minutes for two
      hours, then hourly). When the delivery cannot be read from ``rows``
      (no ``date_field`` for the endpoint, or no usable date in the answer),
      the 15-minute re-check runs for the two hours and the entry then waits
      for the next publication.

    ``now`` defaults to the current time; a naive ``now`` is taken as JST.
    ``business_day`` says whether today is an exchange business day, and
    ``previous_business_day`` (YYYY-MM-DD) is the last one before today; None
    means unknown and falls back to Monday-Friday.
    """
    publication = PUBLICATIONS.get(endpoint)
    if publication is None:
        return TTL_24H
    if now is None:
        now = datetime.now(JST)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=JST)
    now = now.astimezone(JST)
    if business_day is None:
        business_day = now.weekday() < 5
    if previous_business_day is None:
        previous = now.date() - timedelta(days=1)
        while previous.weekday() >= 5:
            previous -= timedelta(days=1)
        previous_business_day = previous.isoformat()

    today_publication = now.replace(
        hour=publication.hour, minute=publication.minute, second=0, microsecond=0
    )
    next_publication = today_publication
    if now >= today_publication:
        next_publication += timedelta(days=1)
    until_next = max(_MIN_TTL_SECONDS, min(int((next_publication - now).total_seconds()), TTL_24H))

    newest = newest_date(rows or (), publication.date_field) if publication.date_field else None
    if not business_day or now < today_publication:
        if newest is not None and newest < previous_business_day:
            return min(LATE_RECHECK_SLOW_SECONDS, until_next)
        return until_next

    in_window = now < today_publication + LATE_WINDOW
    if newest is None:
        return min(LATE_RECHECK_SECONDS, until_next) if in_window else until_next
    if newest >= now.date().isoformat():
        return until_next
    recheck = LATE_RECHECK_SECONDS if in_window else LATE_RECHECK_SLOW_SECONDS
    return min(recheck, until_next)
