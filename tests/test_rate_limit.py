"""Tests for per-user token-bucket rate limiting."""

from __future__ import annotations

import asyncio

import pytest

from jquants_mcp.rate_limit import RateLimiter, RateLimitExceededError


@pytest.mark.asyncio
async def test_allows_up_to_burst():
    rl = RateLimiter(per_minute=60, burst=5)
    for _ in range(5):
        await rl.acquire("alice")


@pytest.mark.asyncio
async def test_rejects_over_burst():
    rl = RateLimiter(per_minute=60, burst=3)
    for _ in range(3):
        await rl.acquire("alice")
    with pytest.raises(RateLimitExceededError) as exc_info:
        await rl.acquire("alice")
    assert exc_info.value.retry_after > 0


@pytest.mark.asyncio
async def test_per_user_isolation():
    rl = RateLimiter(per_minute=60, burst=2)
    await rl.acquire("alice")
    await rl.acquire("alice")
    # bob's bucket is independent
    await rl.acquire("bob")
    await rl.acquire("bob")
    with pytest.raises(RateLimitExceededError):
        await rl.acquire("alice")


@pytest.mark.asyncio
async def test_refill_restores_tokens(monkeypatch):
    rl = RateLimiter(per_minute=60, burst=2)
    t = [1000.0]
    monkeypatch.setattr("jquants_mcp.rate_limit.time.monotonic", lambda: t[0])

    await rl.acquire("alice")
    await rl.acquire("alice")
    with pytest.raises(RateLimitExceededError):
        await rl.acquire("alice")

    # 1.5 seconds later — refill_rate = 1.0 token/s → 1 full token available
    t[0] += 1.5
    await rl.acquire("alice")


@pytest.mark.asyncio
async def test_retry_after_scales_with_refill_rate():
    rl = RateLimiter(per_minute=30, burst=1)  # refill 0.5/s
    await rl.acquire("alice")
    with pytest.raises(RateLimitExceededError) as exc_info:
        await rl.acquire("alice")
    # 1 token needs ~2 seconds at 0.5/s
    assert 1.5 < exc_info.value.retry_after <= 2.01


@pytest.mark.asyncio
async def test_concurrent_acquires_are_serialized():
    rl = RateLimiter(per_minute=60, burst=10)

    async def go():
        try:
            await rl.acquire("alice")
            return True
        except RateLimitExceededError:
            return False

    results = await asyncio.gather(*(go() for _ in range(15)))
    assert sum(results) == 10
    assert results.count(False) == 5


def test_invalid_params():
    with pytest.raises(ValueError):
        RateLimiter(per_minute=0, burst=1)
    with pytest.raises(ValueError):
        RateLimiter(per_minute=60, burst=0)


def test_error_to_dict():
    err = RateLimitExceededError(retry_after=3.5)
    d = err.to_dict()
    assert d["error"] is True
    assert d["error_type"] == "RateLimitExceededError"
    assert d["retry_after"] == 3.5
    assert "hint" in d
