"""Shared test fixtures for jquants-mcp."""

from __future__ import annotations

from pathlib import Path

import pytest

from jquants_mcp.cache.store import CacheStore
from jquants_mcp.config import Settings


@pytest.fixture(autouse=True)
def _reset_server_globals():
    """Reset mutable server-module globals between tests.

    ``_user_clients`` / ``_user_client_last_used`` are process-wide dicts; left
    populated they leak per-user clients across tests and can mask or trigger
    the cached-client fast path in unrelated cases.
    """
    import jquants_mcp.server as server_module

    def _reset() -> None:
        server_module._plan_detected = False
        server_module._user_clients.clear()
        server_module._user_client_last_used.clear()
        server_module._plan_cache.clear()

    _reset()
    yield
    _reset()


@pytest.fixture()
def tmp_cache_dir(tmp_path: Path) -> Path:
    """Provide a temporary cache directory."""
    return tmp_path


@pytest.fixture()
def settings(tmp_cache_dir: Path) -> Settings:
    """Provide test settings with a temporary cache directory."""
    return Settings(
        jquants_api_key="test-api-key-dummy",
        jquants_plan="free",
        jquants_cache_dir=str(tmp_cache_dir),
    )


@pytest.fixture()
def cache_store(tmp_cache_dir: Path) -> CacheStore:
    """Provide a CacheStore with a temporary database."""
    store = CacheStore(tmp_cache_dir / "test_cache.db", default_plan="standard")
    yield store
    store.close()
