"""API key validation and plan detection for multi-user mode."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .exceptions import AuthenticationError, PlanRestrictionError

if TYPE_CHECKING:
    from .client import JQuantsClient

logger = logging.getLogger(__name__)

# API キーの再検証は1日1回
_VALIDATION_INTERVAL = 86400

# 1時間以上アイドル状態のインメモリクライアントを削除
_STALE_CLIENT_TTL = 3600

# ユーザーの実際のプランを検出するプローブエンドポイント（上位プランから順）
# 各タプル: (プラン名, エンドポイントパス, プローブパラメータ)
# J-Quants API v2 ドキュメントで検証済み:
#   /fins/details          → Premium のみ
#   /markets/short-ratio   → Standard / Premium（旧パス: /markets/short_selling）
#   /equities/investor-types → Light / Standard / Premium（旧パス: /markets/trades_spec）
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


async def validate_api_key(client: JQuantsClient) -> bool:
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
        # ネットワークエラーには寛容に対応 — キーを無効化しない
        logger.warning("API key validation encountered a non-auth error: %s", e)
        return True


async def detect_plan(client: JQuantsClient) -> str:
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
