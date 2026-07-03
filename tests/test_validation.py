"""Tests for the validation module (API key validation and plan detection)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from jquants_mcp.exceptions import AuthenticationError, PlanRestrictionError
from jquants_mcp.validation import (
    _VALIDATION_INTERVAL,
    detect_plan,
    needs_validation,
    validate_api_key,
)


# ---------------------------------------------------------------------------
# needs_validation
# ---------------------------------------------------------------------------


def test_needs_validation_none():
    """None last_validated_at always requires validation."""
    assert needs_validation(None) is True


def test_needs_validation_recent():
    """A timestamp from just now does not require validation."""
    recent = int(time.time())
    assert needs_validation(recent) is False


def test_needs_validation_stale():
    """A timestamp older than the validation interval requires re-validation."""
    old = int(time.time()) - _VALIDATION_INTERVAL - 1
    assert needs_validation(old) is True


def test_needs_validation_exactly_at_boundary():
    """Exactly at the interval boundary is considered stale."""
    boundary = int(time.time()) - _VALIDATION_INTERVAL
    assert needs_validation(boundary) is True


# ---------------------------------------------------------------------------
# validate_api_key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_api_key_success():
    """A 200 response from /markets/calendar returns True."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value={"trading_calendar": []})

    result = await validate_api_key(mock_client)

    assert result is True
    mock_client.get.assert_awaited_once_with("/markets/calendar")


@pytest.mark.asyncio
async def test_validate_api_key_auth_error():
    """AuthenticationError from the API is re-raised."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=AuthenticationError("invalid key"))

    with pytest.raises(AuthenticationError):
        await validate_api_key(mock_client)


@pytest.mark.asyncio
async def test_validate_api_key_network_error_is_lenient():
    """Non-auth errors (e.g. network failures) return True (lenient)."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=OSError("connection refused"))

    result = await validate_api_key(mock_client)

    assert result is True


# ---------------------------------------------------------------------------
# detect_plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_plan_premium():
    """When the premium endpoint returns 200, detected plan is 'premium'."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value={"data": []})

    result = await detect_plan(mock_client)

    assert result == "premium"
    # Only the premium probe should have been called
    mock_client.get.assert_awaited_once()
    call_args = mock_client.get.call_args[0]
    assert "details" in call_args[0]


@pytest.mark.asyncio
async def test_detect_plan_standard():
    """When premium returns 403 but standard returns 200, detected plan is 'standard'."""
    call_count = 0

    async def side_effect(path, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if "details" in path:
            raise PlanRestrictionError("forbidden", status_code=403)
        return {"data": []}

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=side_effect)

    result = await detect_plan(mock_client)

    assert result == "standard"
    assert call_count == 2


@pytest.mark.asyncio
async def test_detect_plan_light():
    """When premium and standard return 403 but light returns 200, plan is 'light'."""

    async def side_effect(path, *args, **kwargs):
        if "details" in path or "short-ratio" in path:
            raise PlanRestrictionError("forbidden", status_code=403)
        return {"data": []}

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=side_effect)

    result = await detect_plan(mock_client)

    assert result == "light"


@pytest.mark.asyncio
async def test_detect_plan_free():
    """When all probes return 403, detected plan is 'free'."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=PlanRestrictionError("forbidden", status_code=403))

    result = await detect_plan(mock_client)

    assert result == "free"


@pytest.mark.asyncio
async def test_detect_plan_auth_error_propagates():
    """AuthenticationError during plan detection is re-raised immediately."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=AuthenticationError("invalid key"))

    with pytest.raises(AuthenticationError):
        await detect_plan(mock_client)


@pytest.mark.asyncio
async def test_detect_plan_non_plan_error_propagates():
    """A non-plan probe error (e.g. network timeout, 5xx) must propagate
    instead of being conflated with a genuine 403 plan restriction —
    otherwise a transient API blip during registration would silently
    downgrade a paying user's stored plan to "free" (regression for the
    detect_plan transient-error fix)."""
    call_count = 0

    async def side_effect(path, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise OSError("network error")

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=side_effect)

    with pytest.raises(OSError):
        await detect_plan(mock_client)

    # Must abort on the first ambiguous failure rather than continuing to
    # probe lower tiers and eventually returning a false "free" result.
    assert call_count == 1


@pytest.mark.asyncio
async def test_detect_plan_non_plan_error_after_genuine_restriction_propagates():
    """A non-plan error on a later probe also propagates, even after an
    earlier probe correctly returned a genuine 403."""

    async def side_effect(path, *args, **kwargs):
        if "details" in path:
            raise PlanRestrictionError("forbidden", status_code=403)
        raise OSError("network error")

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=side_effect)

    with pytest.raises(OSError):
        await detect_plan(mock_client)
