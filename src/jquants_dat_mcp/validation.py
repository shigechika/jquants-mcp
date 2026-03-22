"""API key validation and plan detection for multi-user mode."""

from __future__ import annotations

import logging

from .exceptions import AuthenticationError, PlanRestrictionError

logger = logging.getLogger(__name__)

# Re-validate API keys once per day
_VALIDATION_INTERVAL = 86400

# Evict in-memory clients idle for more than 1 hour
_STALE_CLIENT_TTL = 3600

# Probe endpoints to detect the user's actual plan (ordered highest to lowest).
# Each tuple: (plan_name, endpoint_path, probe_params)
# Plans verified against J-Quants API v2 docs:
#   /fins/details          → Premium only
#   /markets/short-ratio   → Standard / Premium  (old path: /markets/short_selling)
#   /equities/investor-types → Light / Standard / Premium  (old path: /markets/trades_spec)
_PLAN_PROBE_ENDPOINTS: list[tuple[str, str, dict]] = [
    ("premium", "/fins/details", {"date": "20240101"}),
    ("standard", "/markets/short-ratio", {"date": "20240101"}),
    ("light", "/equities/investor-types", {}),
]


def needs_validation(last_validated_at: int | None) -> bool:
    """Check if the user's API key needs re-validation.

    Args:
        last_validated_at: Unix timestamp of the last successful validation, or None.

    Returns:
        True if validation is required (never validated or interval elapsed).
    """
    import time

    if last_validated_at is None:
        return True
    return (int(time.time()) - last_validated_at) >= _VALIDATION_INTERVAL


async def validate_api_key(client) -> bool:
    """Verify that the API key is still valid by calling a lightweight endpoint.

    Uses /markets/calendar which is available on all plans.

    Args:
        client: JQuantsClient instance to test.

    Returns:
        True if the key is valid.

    Raises:
        AuthenticationError: If the API key has been revoked (HTTP 401).
    """
    try:
        await client.get("/markets/calendar")
        return True
    except AuthenticationError:
        raise
    except Exception as e:
        # Be lenient on network errors — do not invalidate the key
        logger.warning("API key validation encountered a non-auth error: %s", e)
        return True


async def detect_plan(client) -> str:
    """Detect the user's actual J-Quants plan by probing plan-restricted endpoints.

    Tests endpoints from highest plan to lowest. Returns the first plan whose
    endpoint responds with 200, or "free" if all probes are restricted.

    Args:
        client: JQuantsClient instance to use for probing.

    Returns:
        Detected plan name: "premium", "standard", "light", or "free".

    Raises:
        AuthenticationError: If the API key itself is invalid.
    """
    for plan, endpoint, params in _PLAN_PROBE_ENDPOINTS:
        try:
            await client.get(endpoint, params)
            logger.info("Plan detection: %s probe succeeded → plan=%s", endpoint, plan)
            return plan
        except PlanRestrictionError:
            logger.debug("Plan detection: %s probe returned 403 (plan < %s)", endpoint, plan)
            continue
        except AuthenticationError:
            raise
        except Exception as e:
            logger.debug("Plan detection: %s probe failed with non-plan error: %s", endpoint, e)
            continue
    return "free"
