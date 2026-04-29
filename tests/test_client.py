"""Tests for J-Quants API client."""

from __future__ import annotations

import pytest
import httpx
import respx

from jquants_mcp.client import JQuantsClient
from jquants_mcp.config import Settings
from jquants_mcp.exceptions import (
    APIError,
    AuthenticationError,
    PlanRestrictionError,
    RateLimitError,
)


@pytest.fixture()
def client_settings(tmp_path) -> Settings:
    return Settings(
        jquants_api_key="test-key",
        jquants_base_url="https://api.example.com/v2",
        jquants_plan="premium",
        jquants_cache_dir=str(tmp_path),
        max_retries=2,
        retry_base_delay=0.01,
    )


@pytest.fixture()
def client(client_settings) -> JQuantsClient:
    return JQuantsClient(client_settings)


class TestAuthentication:
    """認証関連のテスト。"""

    async def test_missing_api_key_raises_error(self, tmp_path):
        settings = Settings(
            jquants_api_key="",
            jquants_cache_dir=str(tmp_path),
        )
        c = JQuantsClient(settings)
        with pytest.raises(AuthenticationError, match="JQUANTS_API_KEY"):
            await c.get("/equities/master")

    @respx.mock
    async def test_invalid_api_key_raises_error(self, client):
        respx.get("https://api.example.com/v2/equities/master").respond(401)
        with pytest.raises(AuthenticationError, match="invalid"):
            await client.get("/equities/master")


class TestAPIErrors:
    """API エラーハンドリングのテスト。"""

    @respx.mock
    async def test_403_raises_plan_restriction(self, client):
        respx.get("https://api.example.com/v2/fins/details").respond(403, text="Forbidden")
        with pytest.raises(PlanRestrictionError):
            await client.get("/fins/details")

    @respx.mock
    async def test_429_retries_then_raises(self, client):
        route = respx.get("https://api.example.com/v2/equities/master")
        route.side_effect = [
            httpx.Response(429),
            httpx.Response(429),
        ]
        with pytest.raises(RateLimitError):
            await client.get("/equities/master")

    @respx.mock
    async def test_500_retries_then_raises(self, client):
        route = respx.get("https://api.example.com/v2/equities/master")
        route.side_effect = [
            httpx.Response(500, text="Internal Server Error"),
            httpx.Response(500, text="Internal Server Error"),
        ]
        with pytest.raises(APIError):
            await client.get("/equities/master")

    @respx.mock
    async def test_400_does_not_retry(self, client):
        """4xx (401/403/429 以外) はリトライしない。"""
        route = respx.get("https://api.example.com/v2/equities/master")
        route.respond(400, text="Bad Request")
        with pytest.raises(APIError) as exc_info:
            await client.get("/equities/master")
        assert exc_info.value.status_code == 400
        assert route.call_count == 1

    @respx.mock
    async def test_400_body_included_in_to_dict(self, client):
        """HTTP 400 response body is included in to_dict() output."""
        route = respx.get("https://api.example.com/v2/equities/bars/daily")
        route.respond(400, text='{"message":"date range out of bounds"}')
        with pytest.raises(APIError) as exc_info:
            await client.get("/equities/bars/daily", {"code": "72030", "from": "20190101"})
        d = exc_info.value.to_dict()
        assert d["status_code"] == 400
        assert d["body"] == '{"message":"date range out of bounds"}'


class TestAPIErrorToDict:
    """APIError.to_dict() のテスト。"""

    def test_body_included_when_present(self):
        err = APIError("test", status_code=400, body="some detail")
        d = err.to_dict()
        assert d["body"] == "some detail"

    def test_body_excluded_when_none(self):
        err = APIError("test", status_code=500, body=None)
        d = err.to_dict()
        assert "body" not in d

    def test_body_excluded_when_empty(self):
        err = APIError("test", status_code=400, body="")
        d = err.to_dict()
        assert "body" not in d

    def test_plan_restriction_error_inherits_body(self):
        err = PlanRestrictionError("forbidden", status_code=403, body="plan too low")
        d = err.to_dict()
        assert d["status_code"] == 403
        assert d["body"] == "plan too low"


class TestSuccessfulRequests:
    """正常リクエストのテスト。"""

    @respx.mock
    async def test_get_returns_json(self, client):
        respx.get("https://api.example.com/v2/equities/master").respond(
            200,
            json={"data": [{"Code": "72030", "CoName": "トヨタ自動車"}]},
        )
        result = await client.get("/equities/master", {"code": "72030"})
        assert result["data"][0]["Code"] == "72030"

    @respx.mock
    async def test_none_params_excluded(self, client):
        """None のパラメータはリクエストに含まれないこと。"""
        route = respx.get("https://api.example.com/v2/equities/master").respond(
            200, json={"data": []}
        )
        await client.get("/equities/master", {"code": "72030", "date": None})
        assert "date" not in str(route.calls[0].request.url)


class TestPagination:
    """ページネーションのテスト。"""

    @respx.mock
    async def test_get_all_pages(self, client):
        route = respx.get("https://api.example.com/v2/equities/bars/daily")
        route.side_effect = [
            httpx.Response(
                200,
                json={
                    "data": [{"Date": "2024-01-04"}],
                    "pagination_key": "next-page",
                },
            ),
            httpx.Response(
                200,
                json={
                    "data": [{"Date": "2024-01-05"}],
                },
            ),
        ]
        result = await client.get_all_pages("/equities/bars/daily", {"code": "72030"})
        assert len(result) == 2
        assert result[0]["Date"] == "2024-01-04"
        assert result[1]["Date"] == "2024-01-05"

    @respx.mock
    async def test_max_pages_limit(self, tmp_path):
        """max_pages に達したら打ち切られること。"""
        settings = Settings(
            jquants_api_key="test-key",
            jquants_base_url="https://api.example.com/v2",
            jquants_plan="premium",
            jquants_cache_dir=str(tmp_path),
            max_retries=2,
            retry_base_delay=0.01,
            max_pages=1,
        )
        c = JQuantsClient(settings)

        respx.get("https://api.example.com/v2/test").respond(
            200,
            json={"data": [{"id": 1}], "pagination_key": "more"},
        )
        result = await c.get_all_pages("/test")
        assert len(result) == 1
