"""Shared test fixtures for jquants-dat-mcp."""

from __future__ import annotations

from pathlib import Path

import pytest

from jquants_dat_mcp.cache.store import CacheStore
from jquants_dat_mcp.config import Settings


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
    store = CacheStore(tmp_cache_dir / "test_cache.db")
    yield store
    store.close()
