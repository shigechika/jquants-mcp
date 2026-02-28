"""BearerTokenVerifier のテスト。"""

import pytest

from jquants_dat_mcp.auth import BearerTokenVerifier

VALID_TOKEN = "abc123secret"


@pytest.fixture
def verifier():
    return BearerTokenVerifier(VALID_TOKEN)


@pytest.mark.asyncio
async def test_valid_token(verifier):
    """正しいトークンで AccessToken が返ること。"""
    result = await verifier.verify_token(VALID_TOKEN)
    assert result is not None
    assert result.token == VALID_TOKEN
    assert result.client_id == "bearer"


@pytest.mark.asyncio
async def test_invalid_token(verifier):
    """不正なトークンで None が返ること。"""
    result = await verifier.verify_token("wrong-token")
    assert result is None


@pytest.mark.asyncio
async def test_empty_token(verifier):
    """空文字トークンで None が返ること。"""
    result = await verifier.verify_token("")
    assert result is None
