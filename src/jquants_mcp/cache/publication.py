"""Expire Tier 2 response-cache entries at the next upstream publication.

A fixed 24-hour TTL keeps a snapshot fetched just before the daily publication
for almost a full day after newer data is out (#632). These endpoints publish
once per business day around a fixed time, per the J-Quants data update
schedule (https://jpx-jquants.com/en/spec/data-update), so an entry is kept
until the next such time instead.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .store import TTL_24H

JST = timezone(timedelta(hours=9))

#: (hour, minute) JST of the daily publication. The schedule says "around",
#: so a fetch soon after this time may still see the previous delivery.
PUBLICATION_TIME_JST: dict[str, tuple[int, int]] = {
    "/markets/margin-interest": (16, 0),
    "/markets/margin-alert": (16, 30),
    "/markets/short-ratio": (16, 30),
    "/markets/short-sale-report": (17, 30),
    "/markets/breakdown": (18, 0),
}

#: How long after the nominal publication time a late delivery is still
#: plausible, and how often to re-check during that window.
LATE_WINDOW = timedelta(hours=2)
LATE_RECHECK_SECONDS = 15 * 60

_MIN_TTL_SECONDS = 60


def ttl_until_next_publication(endpoint: str, now: datetime | None = None) -> int:
    """Seconds to cache a response from ``endpoint`` fetched at ``now``.

    Endpoints without a known publication time keep the 24-hour TTL. On a
    weekday within ``LATE_WINDOW`` after the publication time, a short TTL is
    returned so a delivery that arrives late is picked up within minutes.
    Weekends skip that window (nothing is published). Exchange holidays are
    not consulted: on those the entry simply expires and is refetched once.
    """
    publication = PUBLICATION_TIME_JST.get(endpoint)
    if publication is None:
        return TTL_24H
    now = (now or datetime.now(JST)).astimezone(JST)
    hour, minute = publication
    today_publication = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now.weekday() < 5 and today_publication <= now < today_publication + LATE_WINDOW:
        return LATE_RECHECK_SECONDS
    next_publication = today_publication
    if now >= today_publication:
        next_publication += timedelta(days=1)
    seconds = int((next_publication - now).total_seconds())
    return max(_MIN_TTL_SECONDS, min(seconds, TTL_24H))
