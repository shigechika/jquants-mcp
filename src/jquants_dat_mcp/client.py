"""HTTP client for J-Quants API v2 with retry, rate limiting, and pagination."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .config import Settings
from .exceptions import APIError, AuthenticationError, PlanRestrictionError, RateLimitError

logger = logging.getLogger(__name__)

# J-Quants API requires YYYYMMDD format — these params have hyphens stripped before sending
_DATE_KEYS: tuple[str, ...] = (
    "date",
    "from",
    "to",
    "disc_date",
    "disc_date_from",
    "disc_date_to",
    "calc_date",
)


class RateLimiter:
    """Sliding window rate limiter based on asyncio."""

    def __init__(self, max_requests: int, window_seconds: float = 60.0):
        self._max_requests = max_requests
        self._window = window_seconds
        self._timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a request slot is available."""
        async with self._lock:
            now = time.monotonic()
            # ウィンドウ外のタイムスタンプを除去
            self._timestamps = [t for t in self._timestamps if now - t < self._window]

            if len(self._timestamps) >= self._max_requests:
                # 最も古いリクエストがウィンドウ外になるまで待機
                wait_time = self._timestamps[0] + self._window - now
                if wait_time > 0:
                    logger.info("レート制限: %.1f秒待機します", wait_time)
                    await asyncio.sleep(wait_time)

            self._timestamps.append(time.monotonic())


class JQuantsClient:
    """J-Quants API v2 HTTP client."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._client: httpx.AsyncClient | None = None
        self._rate_limiter = RateLimiter(
            max_requests=settings.get_rate_limit(),
            window_seconds=60.0,
        )

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Lazy initialization of httpx.AsyncClient."""
        if self._client is None:
            if not self._settings.jquants_api_key:
                raise AuthenticationError(
                    "JQUANTS_API_KEY が設定されていません。"
                    "環境変数または .env ファイルで設定してください。"
                )
            self._client = httpx.AsyncClient(
                base_url=self._settings.jquants_base_url,
                headers={"x-api-key": self._settings.jquants_api_key},
                timeout=30.0,
            )
        return self._client

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a GET request with rate limiting and retry.

        Args:
            path: API endpoint path (e.g. "/equities/bars/daily")
            params: Query parameters

        Returns:
            Parsed JSON response as dict

        Raises:
            AuthenticationError: API key is missing or invalid
            PlanRestrictionError: Endpoint requires a higher plan
            RateLimitError: Rate limit exceeded after max retries
            APIError: Other API errors
        """
        client = await self._ensure_client()
        params = {k: v for k, v in (params or {}).items() if v is not None}
        for key in _DATE_KEYS:
            if key in params and isinstance(params[key], str):
                params[key] = params[key].replace("-", "")

        last_error: Exception | None = None
        for attempt in range(self._settings.max_retries):
            await self._rate_limiter.acquire()

            try:
                response = await client.get(path, params=params)
            except httpx.HTTPError as e:
                last_error = APIError(f"HTTP 通信エラー: {e}", status_code=0)
                wait = self._settings.retry_base_delay * (2**attempt)
                logger.warning(
                    "通信エラー (試行 %d/%d): %s", attempt + 1, self._settings.max_retries, e
                )
                await asyncio.sleep(wait)
                continue

            if response.status_code == 200:
                return response.json()

            if response.status_code == 401:
                raise AuthenticationError(
                    "API キーが無効です。JQUANTS_API_KEY を確認してください。"
                )

            if response.status_code == 403:
                raise PlanRestrictionError(
                    "このエンドポイントは現在のプランでは利用できません。",
                    status_code=403,
                    body=response.text,
                )

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                wait = (
                    float(retry_after)
                    if retry_after
                    else self._settings.retry_base_delay * (2**attempt)
                )
                logger.warning(
                    "レート制限 (試行 %d/%d): %.1f秒待機",
                    attempt + 1,
                    self._settings.max_retries,
                    wait,
                )
                last_error = RateLimitError(retry_after=wait)
                await asyncio.sleep(wait)
                continue

            # その他のエラー
            last_error = APIError(
                f"API エラー (HTTP {response.status_code})",
                status_code=response.status_code,
                body=response.text,
            )
            if response.status_code >= 500:
                wait = self._settings.retry_base_delay * (2**attempt)
                logger.warning(
                    "サーバーエラー (試行 %d/%d): %d",
                    attempt + 1,
                    self._settings.max_retries,
                    response.status_code,
                )
                await asyncio.sleep(wait)
                continue

            # 4xx (401, 403, 429 以外) はリトライしない
            raise last_error

        # リトライ上限到達
        raise last_error or APIError("リトライ上限に達しました", status_code=0)

    async def get_all_pages(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        data_key: str = "data",
    ) -> list[dict[str, Any]]:
        """Fetch all pages of paginated data.

        Args:
            path: API endpoint path
            params: Query parameters
            data_key: Key in response containing the data array

        Returns:
            Combined list of all data records across pages
        """
        all_data: list[dict[str, Any]] = []
        params = dict(params or {})
        pages_fetched = 0

        while True:
            response = await self.get(path, params)
            records = response.get(data_key, [])
            all_data.extend(records)
            pages_fetched += 1

            pagination_key = response.get("pagination_key")
            if not pagination_key or pages_fetched >= self._settings.max_pages:
                if pagination_key:
                    logger.info(
                        "ページネーション上限 (%d ページ) に到達。残りデータあり。",
                        self._settings.max_pages,
                    )
                break
            params["pagination_key"] = pagination_key

        return all_data

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
