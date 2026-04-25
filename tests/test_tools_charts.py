"""Tests for chart-rendering tools."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

import jquants_mcp.server as server_module
from jquants_mcp.cache.store import CacheStore
from jquants_mcp.client import JQuantsClient
from jquants_mcp.config import Settings

# Skip the entire module when the optional [charts] extras are missing.
mplfinance = pytest.importorskip("mplfinance")


@pytest.fixture()
def mock_env(tmp_path):
    settings = Settings(
        jquants_api_key="test-key",
        jquants_plan="premium",
        jquants_cache_dir=str(tmp_path),
        max_retries=1,
        retry_base_delay=0.01,
    )
    client = JQuantsClient(settings)
    cache = CacheStore(tmp_path / "test.db", default_plan=settings.jquants_plan)

    with (
        patch.object(server_module, "_settings", settings),
        patch.object(server_module, "_client", client),
        patch.object(server_module, "_cache", cache),
    ):
        yield {"settings": settings, "client": client, "cache": cache}

    cache.close()


def _bar(
    code: str,
    date: str,
    *,
    o: float = 100.0,
    h: float = 110.0,
    low: float = 90.0,
    c: float = 105.0,
    vo: float = 1_000.0,
    adj_factor: float = 1.0,
) -> dict:
    return {
        "Code": code,
        "Date": date,
        "O": o,
        "H": h,
        "L": low,
        "C": c,
        "Vo": vo,
        "Va": c * vo,
        "AdjFactor": adj_factor,
        "AdjO": o,
        "AdjH": h,
        "AdjL": low,
        "AdjC": c,
        "AdjVo": vo,
    }


def _seed(cache: CacheStore, rows: list[dict]) -> None:
    cache.put_rows(
        "equities_bars_daily",
        rows,
        key_columns=["Code", "Date"],
        adj_factor_key="AdjFactor",
    )


async def _call_image(tool: str, **kwargs) -> bytes:
    """Invoke a chart tool and return the PNG bytes."""
    result = await server_module.mcp.call_tool(tool, kwargs)
    # FastMCP wraps the Image return as ImageContent (base64 in .data).
    blob = result.content[0]
    raw = blob.data
    if isinstance(raw, str):
        import base64

        raw = base64.b64decode(raw)
    return raw


def _is_png(data: bytes) -> bool:
    return data[:8] == b"\x89PNG\r\n\x1a\n"


def _png_dimensions(data: bytes) -> tuple[int, int]:
    """Return the PNG width and height by parsing the IHDR chunk.

    PNG layout: 8-byte signature + 4-byte length + 4-byte chunk type
    ("IHDR") + 4-byte width + 4-byte height + ... All multi-byte
    integers are big-endian.
    """
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return (width, height)


# A real chart PNG is 1200x800 (figsize 12x8 at DPI 100). The error-image
# fallback in tools/charts.py is 800x200 (figsize 8x2). Use the WIDTH to
# distinguish: matplotlib bbox_inches='tight' on the error image can
# trim the height, but the rendered width stays close to the figsize.
def _is_real_chart_png(data: bytes) -> bool:
    """True for the actual candlestick PNG; False for the error-image PNG.

    Without this check, a test that asserts ``_is_png(...)`` would silently
    pass even when the tool fell back to the error-image path, masking
    real rendering bugs.
    """
    if not _is_png(data):
        return False
    width, _ = _png_dimensions(data)
    # Real chart: 1200 px wide. Error image: 800 px wide. Use a midpoint
    # well above the error width but below the chart width.
    return width > 1000


def _is_error_image_png(data: bytes) -> bool:
    """True for the error-image fallback PNG (small, 800-px wide figure).

    Tests that intentionally exercise the error path use this so a
    regression that makes the tool render a real chart on bad input is
    visible (rather than a generic ``_is_png`` check that accepts both).
    """
    if not _is_png(data):
        return False
    width, _ = _png_dimensions(data)
    return width <= 1000


def _make_fake_png(width: int = 1200, height: int = 800) -> bytes:
    """Build a minimal PNG-looking byte string with the requested dimensions.

    Used by tests that patch ``mpf.plot`` to capture kwargs: the patched
    callable still has to write *something* PNG-shaped to the buffer for
    the tool to surface a successful Image. Encodes width/height into
    the IHDR slot so ``_is_real_chart_png`` / ``_is_error_image_png``
    keep working against the fake.
    """
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00" * 8
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x00" * 100
    )


class TestRenderCandlestick:
    async def test_returns_png_image(self, mock_env):
        rows = []
        for i in range(40):
            d = (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, o=100 + i, h=110 + i, low=95 + i, c=105 + i))
        _seed(mock_env["cache"], rows)

        png = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-01-05",
            to_date="2026-02-13",
        )
        assert _is_real_chart_png(png)
        # Sanity: the rendered chart is a non-trivial size.
        assert 5_000 < len(png) < 500_000

    async def test_with_indicators(self, mock_env):
        rows = []
        for i in range(60):
            d = (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, c=100 + i))
        _seed(mock_env["cache"], rows)

        png = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-01-05",
            to_date="2026-03-05",
            indicators=["volume", "sma20", "bb20"],
        )
        assert _is_real_chart_png(png)

    async def test_unknown_indicator_returns_error_image(self, mock_env):
        # Even a validation error should return a PNG (the contract is
        # "always return an Image"), not a dict.
        png = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-01-05",
            to_date="2026-01-10",
            indicators=["bogus"],
        )
        assert _is_error_image_png(png)

    async def test_unknown_style_returns_error_image(self, mock_env):
        png = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-01-05",
            to_date="2026-01-10",
            style="rainbow",
        )
        assert _is_error_image_png(png)

    async def test_empty_cache_returns_error_image(self, mock_env):
        # No bars seeded — should produce an error PNG explaining how
        # to populate the cache.
        png = await _call_image(
            "render_candlestick",
            code="99999",
            from_date="2026-01-05",
            to_date="2026-01-10",
        )
        assert _is_error_image_png(png)

    async def test_inverted_date_range_returns_error_image(self, mock_env):
        png = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-02-01",
            to_date="2026-01-01",
        )
        assert _is_error_image_png(png)

    async def test_validation_error_returns_error_image(self, mock_env):
        png = await _call_image(
            "render_candlestick",
            code="bad",
            from_date="2026-01-05",
            to_date="2026-01-10",
        )
        assert _is_error_image_png(png)

    async def test_unadjusted_uses_raw_columns(self, mock_env):
        # Seed bars where adjusted prices differ from raw to confirm
        # the `adjusted=False` branch picks up the raw columns.
        rows = []
        for i in range(20):
            d = (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d")
            r = _bar("27800", d, o=100, h=110, low=90, c=105)
            # Adjust the adjusted columns separately.
            r["AdjO"] = 50
            r["AdjH"] = 55
            r["AdjL"] = 45
            r["AdjC"] = 52
            rows.append(r)
        _seed(mock_env["cache"], rows)

        png_adj = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-01-05",
            to_date="2026-01-24",
            adjusted=True,
        )
        png_raw = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-01-05",
            to_date="2026-01-24",
            adjusted=False,
        )
        assert _is_real_chart_png(png_adj)
        assert _is_real_chart_png(png_raw)
        # The two charts should differ — the adjusted one uses 50/55/45/52
        # while the raw one uses 100/110/90/105.
        assert png_adj != png_raw


class TestRenderCandlestickEdgeCases:
    """Robustness tests for inputs that look unusual but actually occur in
    real J-Quants data: single-day windows, indicator windows wider than
    the data, mid-range stock splits, mostly-flat bars, and mixed
    valid/malformed rows.
    """

    async def test_single_bar(self, mock_env):
        # A 1-row chart shouldn't crash; mplfinance handles it but it's
        # an easy way to introduce regressions.
        _seed(mock_env["cache"], [_bar("27800", "2026-04-01")])
        png = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-04-01",
            to_date="2026-04-01",
            indicators=["volume"],  # SMA needs >=N bars; skip for 1-bar test
        )
        assert _is_real_chart_png(png)

    async def test_sma_window_wider_than_data_silently_skips(self, mock_env):
        # With sma60 + only 10 bars, the SMA never has enough history.
        # Current code: skip the addplot. Test pins this so a refactor
        # can't accidentally make it crash or pad the data.
        rows = []
        for i in range(10):
            d = (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d))
        _seed(mock_env["cache"], rows)
        png = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-04-01",
            to_date="2026-04-10",
            indicators=["volume", "sma60"],
        )
        assert _is_real_chart_png(png)

    async def test_all_flat_bars_render(self, mock_env):
        # Every bar has O=H=L=C — produces all-doji candles. Real-world
        # case: low-liquidity stocks, the all-flat segment of a halt or
        # 寄らずストップ days. mplfinance should still emit a valid PNG.
        rows = []
        for i in range(10):
            d = (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("99990", d, o=500, h=500, low=500, c=500, vo=0))
        _seed(mock_env["cache"], rows)
        png = await _call_image(
            "render_candlestick",
            code="99990",
            from_date="2026-04-01",
            to_date="2026-04-10",
            indicators=["volume"],
        )
        assert _is_real_chart_png(png)

    async def test_split_inside_range_adjusted_vs_raw_differ(self, mock_env):
        # Mid-range 2-for-1 split: raw prices halve at the split day;
        # adjusted prices are continuous (Adj* values divide pre-split
        # raw). Both modes should render a valid PNG; the two PNGs
        # must differ because their underlying price series differ.
        rows = []
        # Pre-split: raw 1000, adj 500 (post-split equivalent)
        for i in range(5):
            d = (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
            r = _bar("27800", d, o=1000, h=1010, low=990, c=1000, adj_factor=2.0)
            r["AdjO"] = r["AdjH"] = r["AdjL"] = r["AdjC"] = 500
            rows.append(r)
        # Split day + post: raw and adjusted equal at 500
        for i in range(5, 10):
            d = (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, o=500, h=510, low=490, c=505, adj_factor=1.0))
        _seed(mock_env["cache"], rows)

        png_adj = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-04-01",
            to_date="2026-04-10",
            adjusted=True,
            indicators=["volume"],
        )
        png_raw = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-04-01",
            to_date="2026-04-10",
            adjusted=False,
            indicators=["volume"],
        )
        assert _is_real_chart_png(png_adj)
        assert _is_real_chart_png(png_raw)
        assert png_adj != png_raw

    async def test_malformed_row_skipped_not_crashing(self, mock_env):
        # Cache may carry rows missing OHLC fields (legacy / partial
        # imports). The chart loop catches these and continues; verify
        # one good bar is enough to render.
        rows = [_bar("27800", "2026-04-01")]
        # Inject a bar with the wrong shape directly through put_rows.
        bad = {"Code": "27800", "Date": "2026-04-02", "Vo": 0}
        # Missing O/H/L/C → row.try block in charts.py drops it.
        rows.append(bad)
        _seed(mock_env["cache"], rows)

        png = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-04-01",
            to_date="2026-04-02",
            indicators=["volume"],
        )
        assert _is_real_chart_png(png)


class TestJpConventionDefaults:
    async def test_default_indicators_are_jp(self, mock_env):
        # Defaults should be the JP triplet (5 / 25). Asserting via the
        # rendered PNG hash is brittle; instead verify the call accepts
        # the default request shape.
        rows = [
            _bar("27800", (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d"))
            for i in range(40)
        ]
        _seed(mock_env["cache"], rows)
        # No indicators kwarg → uses the default
        png = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-04-01",
            to_date="2026-05-10",
        )
        assert _is_real_chart_png(png)

    async def test_sma25_and_sma75_are_accepted(self, mock_env):
        # JP convention adds sma25 and sma75 alongside sma5; both must
        # be accepted by validation (no "Unknown indicator" error).
        rows = [
            _bar("27800", (datetime(2026, 1, 1) + timedelta(days=i)).strftime("%Y-%m-%d"))
            for i in range(120)  # enough for sma75
        ]
        _seed(mock_env["cache"], rows)
        png = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-01-01",
            to_date="2026-04-30",
            indicators=["volume", "sma5", "sma25", "sma75"],
        )
        assert _is_real_chart_png(png)


def _seed_master(cache: CacheStore, code: str, name: str | None, date: str = "2026-01-04") -> None:
    """Seed an ``equities_master`` row so ``_get_company_name`` can find it.

    J-Quants API v2 uses the short-form key ``CoName`` (not the longer
    ``CompanyName`` shown in some doc pages); the cache stores the
    response verbatim so the seed must use the same key.
    """
    row = {"Code": code, "Date": date, "CoName": name}
    cache.put_rows("equities_master", [row], key_columns=["Code", "Date"])


class TestChartTitleHelpers:
    """Unit tests for ``_get_company_name`` and ``_build_chart_title``.

    Title strings cannot easily be read back from a rendered PNG without
    OCR, so the title-construction logic is extracted into pure helpers
    that can be tested directly. The chart-rendering path then composes
    the final title via these helpers.
    """

    def test_build_title_without_company(self):
        from jquants_mcp.tools.charts import _build_chart_title

        title = _build_chart_title("7203", None, "2026-01-05", "2026-01-30", True)
        assert "7203" in title
        assert "2026-01-05" in title
        assert "2026-01-30" in title
        assert "adjusted" in title

    def test_build_title_with_company(self):
        from jquants_mcp.tools.charts import _build_chart_title

        title = _build_chart_title("7203", "トヨタ自動車", "2026-01-05", "2026-01-30", True)
        assert "7203" in title
        assert "トヨタ自動車" in title

    def test_build_title_raw_mode(self):
        from jquants_mcp.tools.charts import _build_chart_title

        title = _build_chart_title("7203", None, "2026-01-05", "2026-01-30", False)
        assert "raw" in title
        assert "adjusted" not in title

    def test_display_code_keeps_4_digit(self):
        from jquants_mcp.tools.charts import _display_code

        assert _display_code("7203") == "7203"

    def test_display_code_collapses_5_digit_ordinary(self):
        from jquants_mcp.tools.charts import _display_code

        # 5-digit ending in 0 = ordinary share → display 4-digit.
        assert _display_code("72030") == "7203"
        assert _display_code("13010") == "1301"

    def test_display_code_keeps_5_digit_non_ordinary(self):
        from jquants_mcp.tools.charts import _display_code

        # 5-digit not ending in 0 = preferred / second-class share.
        assert _display_code("25935") == "25935"
        assert _display_code("99991") == "99991"

    async def test_render_title_uses_4_digit_form(self, mock_env):
        # 5-digit ordinary share input should appear as 4-digit in the
        # title, paired with the company name.
        rows = [
            _bar(
                "27800",
                (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d"),
            )
            for i in range(10)
        ]
        _seed(mock_env["cache"], rows)
        _seed_master(mock_env["cache"], "27800", "テスト株式会社")

        captured: dict = {}

        def fake_plot(df, **kwargs):
            captured.update(kwargs)
            kwargs["savefig"]["fname"].write(_make_fake_png())

        with patch("mplfinance.plot", side_effect=fake_plot):
            await _call_image(
                "render_candlestick",
                code="27800",  # caller-supplied 5-digit form
                from_date="2026-04-01",
                to_date="2026-04-10",
                indicators=["volume"],
            )

        # 27800 is an ordinary share → display as "2780" (drops the 0).
        # ``_build_chart_title`` puts the company name after a single
        # space, so "2780 " is always the prefix in this case.
        title = captured.get("title", "")
        assert title.startswith("2780 ")
        assert "27800" not in title  # full 5-digit form must NOT appear
        assert "テスト株式会社" in title

    async def test_get_company_name_from_master(self, mock_env):
        from jquants_mcp.tools.charts import _get_company_name

        _seed_master(mock_env["cache"], "27800", "テスト株式会社")
        assert _get_company_name(mock_env["cache"], "27800") == "テスト株式会社"

    async def test_get_company_name_returns_most_recent(self, mock_env):
        from jquants_mcp.tools.charts import _get_company_name

        # Older row + newer row with a different name; the newer should win.
        _seed_master(mock_env["cache"], "27800", "旧社名", "2024-01-04")
        _seed_master(mock_env["cache"], "27800", "新社名", "2026-01-04")
        assert _get_company_name(mock_env["cache"], "27800") == "新社名"

    async def test_get_company_name_falls_back_to_english(self, mock_env):
        from jquants_mcp.tools.charts import _get_company_name

        # Some master rows only have the English name (e.g. some new
        # listings before the JP name is filled in). J-Quants uses the
        # short-form key ``CoNameEn`` for the English name.
        cache = mock_env["cache"]
        cache.put_rows(
            "equities_master",
            [{"Code": "27800", "Date": "2026-01-04", "CoNameEn": "Toyota Motor"}],
            key_columns=["Code", "Date"],
        )
        assert _get_company_name(cache, "27800") == "Toyota Motor"

    async def test_get_company_name_returns_none_when_master_missing(self, mock_env):
        from jquants_mcp.tools.charts import _get_company_name

        # No master row at all → must return None, never raise.
        assert _get_company_name(mock_env["cache"], "99999") is None

    async def test_get_company_name_returns_none_for_blank_field(self, mock_env):
        from jquants_mcp.tools.charts import _get_company_name

        # Real cache sometimes has rows where CompanyName is None
        # (observed for code 42880). Must not return the empty value.
        _seed_master(mock_env["cache"], "27800", None)
        assert _get_company_name(mock_env["cache"], "27800") is None

    async def test_render_uses_company_name_when_available(self, mock_env):
        # End-to-end: with a master row present, the rendered chart uses
        # the company-name-bearing title. We can't OCR the PNG directly,
        # so instead we patch mpf.plot to capture the title kwarg.
        rows = [
            _bar("27800", (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d"))
            for i in range(10)
        ]
        _seed(mock_env["cache"], rows)
        _seed_master(mock_env["cache"], "27800", "テスト株式会社")

        captured: dict = {}

        def fake_plot(df, **kwargs):
            captured.update(kwargs)
            # Still write a valid PNG to the buffer so the tool succeeds.
            kwargs["savefig"]["fname"].write(_make_fake_png())

        with patch("mplfinance.plot", side_effect=fake_plot):
            await _call_image(
                "render_candlestick",
                code="27800",
                from_date="2026-04-01",
                to_date="2026-04-10",
                indicators=["volume"],
            )

        assert "テスト株式会社" in captured.get("title", "")


def test_register_no_op_when_extras_missing():
    """register() should silently skip when mplfinance isn't importable.

    Simulated by patching the import to raise. Confirms the lean stdio
    profile (no [charts] extras) starts cleanly.
    """
    import builtins
    from unittest.mock import MagicMock

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mplfinance":
            raise ModuleNotFoundError("simulated missing mplfinance")
        return real_import(name, *args, **kwargs)

    from jquants_mcp.tools import charts as charts_module

    mcp_stub = MagicMock()
    with patch.object(builtins, "__import__", side_effect=fake_import):
        # Should not raise.
        charts_module.register(mcp_stub, lambda: None, lambda: None)
    # And the tool decorator was never invoked.
    mcp_stub.tool.assert_not_called()
