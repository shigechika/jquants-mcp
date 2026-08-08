"""Regression tests for the shared-connection race (#537).

One ``sqlite3.Connection`` is shared by the event loop and by worker threads
(the connection is opened with ``check_same_thread=False``). Unsynchronised
concurrent writes through it surface as ``cannot start a transaction within a
transaction`` or ``bad parameter or other API misuse`` — errors that reach the
MCP client as a failed tool call, and that no single-threaded test would ever
produce.
"""

from __future__ import annotations

import asyncio
import inspect
import threading

import pytest

import jquants_mcp.server as server_module
from jquants_mcp.cache.store import CacheStore

WRITES = 300


class TestConcurrentWrites:
    """The store must tolerate a worker thread and the loop writing at once."""

    def test_thread_and_loop_writers_do_not_corrupt_the_connection(self, cache_store: CacheStore):
        errors: list[str] = []

        def thread_writer() -> None:
            for i in range(WRITES):
                try:
                    cache_store.put_response(f"race-t-{i}", {"i": i}, ttl_seconds=60)
                except Exception as exc:  # noqa: BLE001 - the failure mode under test
                    errors.append(f"thread: {type(exc).__name__}: {exc}")
                    return

        async def loop_writer() -> None:
            for i in range(WRITES):
                try:
                    cache_store.put_response(f"race-l-{i}", {"i": i}, ttl_seconds=60)
                except Exception as exc:  # noqa: BLE001 - the failure mode under test
                    errors.append(f"loop: {type(exc).__name__}: {exc}")
                    return
                await asyncio.sleep(0)

        async def run() -> None:
            worker = threading.Thread(target=thread_writer)
            worker.start()
            await loop_writer()
            worker.join()

        asyncio.run(run())

        assert not errors, f"concurrent writes broke the shared connection: {errors[:3]}"
        assert cache_store.get_response(f"race-t-{WRITES - 1}") == {"i": WRITES - 1}
        assert cache_store.get_response(f"race-l-{WRITES - 1}") == {"i": WRITES - 1}

    def test_concurrent_threads_writing_rows(self, cache_store: CacheStore):
        """put_rows holds a transaction across many inserts — the widest window."""
        errors: list[str] = []

        def writer(offset: int) -> None:
            rows = [{"Code": f"{offset}{i:04d}", "Date": "2026-07-24", "x": i} for i in range(50)]
            for _ in range(5):
                try:
                    cache_store.put_rows(
                        "equities_earnings_calendar", rows, key_columns=["Code", "Date"]
                    )
                except Exception as exc:  # noqa: BLE001 - the failure mode under test
                    errors.append(f"{type(exc).__name__}: {exc}")
                    return

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(1, 5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"concurrent put_rows broke the shared connection: {errors[:3]}"


#: Tools deliberately left synchronous. FastMCP runs a sync body in a worker
#: thread, which is what we want for these: they do blocking work (lazy
#: initialization with migrations, COUNT(*) over a multi-GB database, bulk
#: DELETE) that would stall every other request if it ran on the event loop.
#: Sharing the connection from that thread is safe because CacheStore
#: serializes its mutating paths — see #537.
BLOCKING_BY_DESIGN = {"health_check", "cache_status", "cache_clear"}


class TestSyncToolInventory:
    """A new sync tool must be a deliberate decision, not an accident.

    Sync means "runs in a worker thread, concurrently with the event loop".
    That is fine for the blocking-by-design tools above, and a trap for
    anything that writes: the shared connection is only safe through the
    store's locked API.
    """

    async def test_only_known_tools_are_sync(self):
        # ``mcp.list_tools()`` returns the wire-format ``mcp.types.Tool`` (no
        # ``.fn``); the internal registry (``_tool_manager``) still exposes the
        # underlying callable for this kind of introspection.
        tools = server_module.mcp._tool_manager.list_tools()
        sync = {t.name for t in tools if not inspect.iscoroutinefunction(t.fn)}
        unexpected = sorted(sync - BLOCKING_BY_DESIGN)
        assert not unexpected, (
            f"new sync tool(s): {unexpected}. FastMCP runs these in a worker thread "
            "alongside the event loop; make the tool async unless it does blocking "
            "work, and route all cache access through CacheStore (#537)."
        )

    async def test_blocking_tools_stay_off_the_event_loop(self):
        tools = {t.name: t for t in server_module.mcp._tool_manager.list_tools()}
        regressed = sorted(
            name
            for name in BLOCKING_BY_DESIGN
            if name in tools and inspect.iscoroutinefunction(tools[name].fn)
        )
        assert not regressed, (
            f"{regressed} became async: their bodies block (migrations, full-table "
            "counts, bulk delete), so on the event loop they stall every other request."
        )


class TestSingleFlightRefresh:
    """Concurrent callers must trigger one upstream fetch, not one each.

    Three tools share the earnings live-refresh fallback. Before the gate, a
    burst of requests each fetched the same upstream payload and wrote it back
    — duplicated API cost, and the widest possible window for concurrent
    writes through the shared connection.
    """

    async def test_live_refresh_fetches_once_for_concurrent_callers(self, tmp_path, monkeypatch):
        from unittest.mock import patch

        from jquants_mcp.config import Settings

        settings = Settings(
            jquants_api_key="test-key",
            jquants_plan="premium",
            jquants_cache_dir=str(tmp_path),
            max_retries=1,
            retry_base_delay=0.01,
        )
        from jquants_mcp.client import JQuantsClient

        client = JQuantsClient(settings)
        cache = CacheStore(tmp_path / "single-flight.db", default_plan="premium")

        calls = 0

        async def fake_get_all_pages(path, params=None):
            nonlocal calls
            calls += 1
            # Yield control so every waiting caller gets a chance to race.
            await asyncio.sleep(0.01)
            return [{"Code": "72030", "Date": "2026-07-27", "CoName": "x"}]

        monkeypatch.setattr(client, "get_all_pages", fake_get_all_pages)

        with (
            patch.object(server_module, "_settings", settings),
            patch.object(server_module, "_client", client),
            patch.object(server_module, "_cache", cache),
        ):
            # A code with no cached rows forces the live-refresh fallback.
            await asyncio.gather(
                *(
                    server_module.mcp.call_tool("get_equities_earnings_calendar", {"code": "99990"})
                    for _ in range(5)
                )
            )

        cache.close()
        assert calls == 1, f"expected a single upstream fetch, got {calls}"


@pytest.fixture
def cache_store(tmp_path):
    store = CacheStore(tmp_path / "race.db", default_plan="premium")
    yield store
    store.close()
