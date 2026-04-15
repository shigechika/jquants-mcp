"""Per-user token-bucket rate limiting.

In-memory and per-instance. Cross-instance state is intentionally out of
scope — see #79 for the trade-off. Revisit if we move beyond single-instance
deployments in practice.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .exceptions import JQuantsDatMCPError


class RateLimitExceededError(JQuantsDatMCPError):
    """The authenticated user exceeded their per-minute request quota."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Rate limit exceeded. Retry after {retry_after:.1f} seconds.")
        self.retry_after = retry_after

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["retry_after"] = self.retry_after
        d["hint"] = "You are sending requests too quickly. Wait and try again."
        return d


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class RateLimiter:
    """Token bucket keyed by user_id.

    - ``capacity`` tokens = burst allowance (short spikes permitted).
    - ``refill_rate`` tokens per second = steady-state request rate.
    - Typical tuning: 60 req/min with burst 20 → refill_rate=1.0, capacity=20.
    """

    def __init__(self, per_minute: int, burst: int) -> None:
        if per_minute <= 0 or burst <= 0:
            raise ValueError("per_minute and burst must be positive")
        self.refill_rate = per_minute / 60.0
        self.capacity = float(burst)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, user_id: str) -> None:
        """Consume one token for the user, or raise RateLimitExceededError.

        Raises:
            RateLimitExceededError: with ``retry_after`` set to the number of
                seconds until the next token is available.
        """
        now = time.monotonic()
        async with self._lock:
            bucket = self._buckets.get(user_id)
            if bucket is None:
                bucket = _Bucket(tokens=self.capacity, last_refill=now)
                self._buckets[user_id] = bucket
            else:
                elapsed = now - bucket.last_refill
                bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_rate)
                bucket.last_refill = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return

            retry_after = (1.0 - bucket.tokens) / self.refill_rate

        raise RateLimitExceededError(retry_after=retry_after)

    def evict(self, user_id: str) -> None:
        self._buckets.pop(user_id, None)
