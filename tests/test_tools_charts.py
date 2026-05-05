"""Tests for chart-rendering tools."""

from __future__ import annotations

import builtins
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

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

    Uses HEIGHT as the discriminator now that aspect_ratio="square" is the
    default (800×800 px): error image is ~200 px tall (figsize 8×2), real
    charts are at least 600 px tall across all aspect ratios.
    """
    if not _is_png(data):
        return False
    _, height = _png_dimensions(data)
    return height > 400


def _is_error_image_png(data: bytes) -> bool:
    """True for the error-image fallback PNG (figsize 8×2, ~200 px tall).

    Tests that intentionally exercise the error path use this so a
    regression that makes the tool render a real chart on bad input is
    visible (rather than a generic ``_is_png`` check that accepts both).
    """
    if not _is_png(data):
        return False
    _, height = _png_dimensions(data)
    return height <= 400


def _is_real_comparison_png(data: bytes) -> bool:
    """True for a real render_comparison_chart PNG.

    The comparison chart default is square (8×8 in = 800×800 px), which has
    the same width as the error image (800 px).  Distinguish by HEIGHT:
    real charts are at least 600 px tall; the error image is ~200 px tall
    (figsize 8×2 with bbox_inches='tight').
    """
    if not _is_png(data):
        return False
    _, height = _png_dimensions(data)
    return height > 400


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

    async def test_alphanumeric_code_accepted_end_to_end(self, mock_env):
        # Issue #150 regression — alphanumeric codes like 130A0 must
        # pass validate_code, reach the rendering path, and produce
        # a real chart PNG (not the validator-error fallback image).
        rows = []
        for i in range(10):
            d = (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("130A0", d))
        _seed(mock_env["cache"], rows)

        png = await _call_image(
            "render_candlestick",
            code="130A0",
            from_date="2026-04-01",
            to_date="2026-04-10",
            indicators=["volume"],
        )
        assert _is_real_chart_png(png)

    async def test_4char_alphanumeric_code_accepted_end_to_end(self, mock_env):
        # Issue #153 regression — the 4-char display form (e.g. 130A)
        # must also pass validate_code, get normalised to 130A0 by
        # _normalize_code, and reach the rendering path. Cache stores
        # under the 5-char form, so seeding with 130A0 and querying
        # with 130A exercises the normalisation round-trip.
        rows = []
        for i in range(10):
            d = (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("130A0", d))
        _seed(mock_env["cache"], rows)

        png = await _call_image(
            "render_candlestick",
            code="130A",  # caller-supplied 4-char display form
            from_date="2026-04-01",
            to_date="2026-04-10",
            indicators=["volume"],
        )
        assert _is_real_chart_png(png)

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

    async def test_square_aspect_ratio_renders(self, mock_env):
        rows = [_bar("27800", f"2026-04-{d:02d}") for d in range(1, 11)]
        _seed(mock_env["cache"], rows)
        png = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-04-01",
            to_date="2026-04-10",
            indicators=["volume"],
            aspect_ratio="square",
        )
        assert _is_real_chart_png(png)

    async def test_landscape_aspect_ratio_renders(self, mock_env):
        rows = [_bar("27800", f"2026-04-{d:02d}") for d in range(1, 11)]
        _seed(mock_env["cache"], rows)
        png = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-04-01",
            to_date="2026-04-10",
            indicators=["volume"],
            aspect_ratio="landscape",
        )
        assert _is_real_chart_png(png)

    async def test_portrait_aspect_ratio_renders(self, mock_env):
        rows = [_bar("27800", f"2026-04-{d:02d}") for d in range(1, 11)]
        _seed(mock_env["cache"], rows)
        png = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-04-01",
            to_date="2026-04-10",
            indicators=["volume"],
            aspect_ratio="portrait",
        )
        assert _is_real_chart_png(png)

    async def test_unknown_aspect_ratio_returns_error(self, mock_env):
        png = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-04-01",
            to_date="2026-04-10",
            aspect_ratio="widescreen",
        )
        assert _is_error_image_png(png)


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

        title = _build_chart_title("7203", None, "2026-01-05", "2026-01-30")
        assert "7203" in title
        assert "2026-01-05" in title
        assert "2026-01-30" in title

    def test_build_title_with_company(self):
        from jquants_mcp.tools.charts import _build_chart_title

        title = _build_chart_title("7203", "トヨタ自動車", "2026-01-05", "2026-01-30")
        assert "7203" in title
        assert "トヨタ自動車" in title

    def test_build_title_omits_adjusted_suffix(self):
        # Industry convention (Kabutan / Yahoo! Finance Japan / TradingView /
        # JP brokerages) — chart titles never carry an "(adjusted)" or
        # "(raw)" suffix. Pin this so the suffix doesn't sneak back in.
        from jquants_mcp.tools.charts import _build_chart_title

        title = _build_chart_title("7203", None, "2026-01-05", "2026-01-30")
        assert "adjusted" not in title
        assert "raw" not in title
        assert "(" not in title  # no parenthesised suffix at all

    def test_display_code_keeps_4_digit(self):
        from jquants_mcp.validators import display_code as _display_code

        assert _display_code("7203") == "7203"

    def test_display_code_collapses_5_digit_ordinary(self):
        from jquants_mcp.validators import display_code as _display_code

        # 5-digit ending in 0 = ordinary share → display 4-digit.
        assert _display_code("72030") == "7203"
        assert _display_code("13010") == "1301"

    def test_display_code_keeps_5_digit_non_ordinary(self):
        from jquants_mcp.validators import display_code as _display_code

        # 5-digit not ending in 0 = preferred / second-class share.
        assert _display_code("25935") == "25935"
        assert _display_code("99991") == "99991"

    def test_display_code_collapses_alphanumeric_ordinary_share(self):
        # JPX's 2024-introduced alphanumeric codes (DDDUD pattern, e.g.
        # 130A0) follow the same display convention as legacy numeric
        # codes: 5-char API form, 4-char display form. Kabutan / Yahoo!
        # Finance Japan / JPX all show ``130A`` (not ``130A0``) for the
        # ordinary share — match that.
        from jquants_mcp.validators import display_code as _display_code

        assert _display_code("130A0") == "130A"
        assert _display_code("554A0") == "554A"

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

    async def test_render_title_uses_4char_form_for_alphanumeric(self, mock_env):
        # Issue #153 / PR #154 follow-through: when the caller supplies
        # the 4-char alphanumeric form (e.g. ``130A``), the cache stores
        # the 5-char form (``130A0``) and the chart title must show the
        # 4-char display form, matching JPX / Kabutan / Yahoo! Finance
        # Japan / TradingView convention. This pins the round-trip:
        # input 130A → normalise to 130A0 → cache hit → display 130A.
        rows = [
            _bar("130A0", (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d"))
            for i in range(10)
        ]
        _seed(mock_env["cache"], rows)
        _seed_master(mock_env["cache"], "130A0", "Veritas In Silico")

        captured: dict = {}

        def fake_plot(df, **kwargs):
            captured.update(kwargs)
            kwargs["savefig"]["fname"].write(_make_fake_png())

        with patch("mplfinance.plot", side_effect=fake_plot):
            await _call_image(
                "render_candlestick",
                code="130A",  # caller-supplied 4-char display form
                from_date="2026-04-01",
                to_date="2026-04-10",
                indicators=["volume"],
            )

        title = captured.get("title", "")
        assert title.startswith("130A ")
        assert "130A0" not in title  # 5-char API form must NOT appear
        assert "Veritas In Silico" in title

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


class TestDetectLockDays:
    """Unit tests for the pure ``_detect_lock_days`` helper."""

    def test_empty_rows(self):
        from jquants_mcp.tools.charts import _detect_lock_days

        assert _detect_lock_days([], adjusted=True) == []

    def test_lock_high_detected(self):
        from jquants_mcp.tools.charts import _detect_lock_days

        rows = [
            {
                "Date": "2026-04-24",
                "Code": "42880",
                "O": 940,
                "H": 940,
                "L": 940,
                "C": 940,
                "AdjO": 940,
                "AdjH": 940,
                "AdjL": 940,
                "AdjC": 940,
                "UpperLimit": "1",
                "LowerLimit": "0",
            }
        ]
        result = _detect_lock_days(rows, adjusted=True)
        assert result == [{"date": "2026-04-24", "direction": "high", "price": 940.0}]

    def test_lock_low_detected(self):
        from jquants_mcp.tools.charts import _detect_lock_days

        rows = [
            {
                "Date": "2026-04-24",
                "Code": "74260",
                "O": 500,
                "H": 500,
                "L": 500,
                "C": 500,
                "AdjO": 500,
                "AdjH": 500,
                "AdjL": 500,
                "AdjC": 500,
                "UpperLimit": "0",
                "LowerLimit": "1",
            }
        ]
        result = _detect_lock_days(rows, adjusted=True)
        assert result == [{"date": "2026-04-24", "direction": "low", "price": 500.0}]

    def test_limit_hit_with_volume_not_lock(self):
        # 4288 アズジェント 4/24 — UL=1 かつ出来高あり (O≠H)。lock ではない
        # ので marker 描画対象から除外されることを確認。
        from jquants_mcp.tools.charts import _detect_lock_days

        rows = [
            {
                "Date": "2026-04-24",
                "Code": "42880",
                "O": 800,
                "H": 940,
                "L": 760,
                "C": 940,
                "AdjO": 800,
                "AdjH": 940,
                "AdjL": 760,
                "AdjC": 940,
                "UpperLimit": "1",
                "LowerLimit": "0",
            }
        ]
        assert _detect_lock_days(rows, adjusted=True) == []

    def test_flat_without_limit_flag_not_lock(self):
        # O=H=L=C だが UpperLimit / LowerLimit ともに 0 — 低流動性の
        # 普通の flat day で、lock ではない。
        from jquants_mcp.tools.charts import _detect_lock_days

        rows = [
            {
                "Date": "2026-04-24",
                "Code": "99990",
                "O": 500,
                "H": 500,
                "L": 500,
                "C": 500,
                "AdjO": 500,
                "AdjH": 500,
                "AdjL": 500,
                "AdjC": 500,
                "UpperLimit": "0",
                "LowerLimit": "0",
            }
        ]
        assert _detect_lock_days(rows, adjusted=True) == []

    def test_adjusted_uses_adj_columns(self):
        # Raw OHLC が flat だが Adj OHLC が non-flat（split-day）。
        # adjusted=True なら lock 判定されない。
        from jquants_mcp.tools.charts import _detect_lock_days

        rows = [
            {
                "Date": "2026-04-24",
                "Code": "27800",
                "O": 100,
                "H": 100,
                "L": 100,
                "C": 100,
                "AdjO": 50,
                "AdjH": 60,
                "AdjL": 40,
                "AdjC": 55,
                "UpperLimit": "1",
                "LowerLimit": "0",
            }
        ]
        # adjusted=True: AdjOHLC が flat ではないので lock 判定なし
        assert _detect_lock_days(rows, adjusted=True) == []
        # adjusted=False: raw OHLC が flat + UL=1 なので lock 判定あり
        result = _detect_lock_days(rows, adjusted=False)
        assert len(result) == 1
        assert result[0]["direction"] == "high"

    def test_malformed_row_skipped(self):
        # OHLC のどれかが欠損していたら crash せず skip。
        from jquants_mcp.tools.charts import _detect_lock_days

        rows = [
            {"Date": "2026-04-24", "Code": "X", "UpperLimit": "1"},  # OHLC 全欠損
            {
                "Date": "2026-04-25",
                "Code": "X",
                "O": 100,
                "H": 100,
                "L": 100,
                "C": 100,
                "AdjO": 100,
                "AdjH": 100,
                "AdjL": 100,
                "AdjC": 100,
                "UpperLimit": "1",
                "LowerLimit": "0",
            },
        ]
        result = _detect_lock_days(rows, adjusted=True)
        assert result == [{"date": "2026-04-25", "direction": "high", "price": 100.0}]

    def test_int_one_also_recognised(self):
        # 防御的: UpperLimit が int 1 で来ても文字列 "1" 同等に扱う。
        from jquants_mcp.tools.charts import _detect_lock_days

        rows = [
            {
                "Date": "2026-04-24",
                "Code": "X",
                "O": 100,
                "H": 100,
                "L": 100,
                "C": 100,
                "AdjO": 100,
                "AdjH": 100,
                "AdjL": 100,
                "AdjC": 100,
                "UpperLimit": 1,  # int, not str
                "LowerLimit": 0,
            }
        ]
        result = _detect_lock_days(rows, adjusted=True)
        assert len(result) == 1
        assert result[0]["direction"] == "high"

    def test_short_form_ul_ll_recognised(self):
        # cache.store rewrites ``UpperLimit`` → ``UL`` and
        # ``LowerLimit`` → ``LL``, so rows pulled from the cache
        # carry the short form. The detector must accept it.
        from jquants_mcp.tools.charts import _detect_lock_days

        rows = [
            {
                "Date": "2026-04-24",
                "Code": "X",
                "O": 100,
                "H": 100,
                "L": 100,
                "C": 100,
                "AdjO": 100,
                "AdjH": 100,
                "AdjL": 100,
                "AdjC": 100,
                "UL": "1",
                "LL": "0",
            }
        ]
        result = _detect_lock_days(rows, adjusted=True)
        assert len(result) == 1
        assert result[0]["direction"] == "high"

    def test_multiple_lock_days_mixed(self):
        from jquants_mcp.tools.charts import _detect_lock_days

        rows = []
        # day 1: lock high
        rows.append(
            {
                "Date": "2026-04-01",
                "Code": "X",
                "O": 100,
                "H": 100,
                "L": 100,
                "C": 100,
                "AdjO": 100,
                "AdjH": 100,
                "AdjL": 100,
                "AdjC": 100,
                "UpperLimit": "1",
                "LowerLimit": "0",
            }
        )
        # day 2: ordinary (non-lock)
        rows.append(
            {
                "Date": "2026-04-02",
                "Code": "X",
                "O": 100,
                "H": 110,
                "L": 90,
                "C": 105,
                "AdjO": 100,
                "AdjH": 110,
                "AdjL": 90,
                "AdjC": 105,
                "UpperLimit": "0",
                "LowerLimit": "0",
            }
        )
        # day 3: lock low
        rows.append(
            {
                "Date": "2026-04-03",
                "Code": "X",
                "O": 80,
                "H": 80,
                "L": 80,
                "C": 80,
                "AdjO": 80,
                "AdjH": 80,
                "AdjL": 80,
                "AdjC": 80,
                "UpperLimit": "0",
                "LowerLimit": "1",
            }
        )
        result = _detect_lock_days(rows, adjusted=True)
        assert len(result) == 2
        assert result[0]["date"] == "2026-04-01" and result[0]["direction"] == "high"
        assert result[1]["date"] == "2026-04-03" and result[1]["direction"] == "low"


class TestRenderCandlestickLockDayOverlay:
    """End-to-end smoke tests for the lock-day horizontal-bar overlay."""

    async def test_render_with_lock_high_returns_real_chart(self, mock_env):
        # Seed a normal-bar window with one 寄らずストップ高 day at the end.
        rows = []
        for i in range(10):
            d = (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("42880", d, o=600, h=620, low=580, c=610))
        # Lock day: O=H=L=C, UpperLimit=1
        lock_day = _bar("42880", "2026-04-15", o=940, h=940, low=940, c=940)
        lock_day["AdjO"] = lock_day["AdjH"] = lock_day["AdjL"] = lock_day["AdjC"] = 940
        lock_day["UL"] = "1"
        lock_day["LL"] = "0"
        rows.append(lock_day)
        _seed(mock_env["cache"], rows)

        png = await _call_image(
            "render_candlestick",
            code="42880",
            from_date="2026-04-01",
            to_date="2026-04-15",
            indicators=["volume"],
        )
        # Must be a real chart PNG (not the error-image fallback).
        assert _is_real_chart_png(png)

    async def test_render_with_lock_low_returns_real_chart(self, mock_env):
        rows = []
        for i in range(10):
            d = (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("74260", d, o=600, h=620, low=580, c=610))
        lock_day = _bar("74260", "2026-04-15", o=400, h=400, low=400, c=400)
        lock_day["AdjO"] = lock_day["AdjH"] = lock_day["AdjL"] = lock_day["AdjC"] = 400
        lock_day["UL"] = "0"
        lock_day["LL"] = "1"
        rows.append(lock_day)
        _seed(mock_env["cache"], rows)

        png = await _call_image(
            "render_candlestick",
            code="74260",
            from_date="2026-04-01",
            to_date="2026-04-15",
            indicators=["volume"],
        )
        assert _is_real_chart_png(png)

    async def test_lock_day_rendering_uses_returnfig_path(self, mock_env):
        # Patch mpf.plot to capture how it was called when a lock day is present.
        # The returnfig branch must be taken (returnfig=True, no savefig).
        rows = []
        for i in range(5):
            d = (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("42880", d, o=600, h=620, low=580, c=610))
        lock_day = _bar("42880", "2026-04-06", o=940, h=940, low=940, c=940)
        lock_day["AdjO"] = lock_day["AdjH"] = lock_day["AdjL"] = lock_day["AdjC"] = 940
        lock_day["UpperLimit"] = "1"
        rows.append(lock_day)
        _seed(mock_env["cache"], rows)

        captured: dict = {}

        def fake_plot(df, **kwargs):
            captured.update(kwargs)
            # Return a minimal fig + axes triple. The first axis must accept
            # ``hlines`` and ``savefig``-able fig.
            fig = MagicMock()
            ax = MagicMock()

            def fake_savefig(buf, **_kw):
                buf.write(_make_fake_png())

            fig.savefig.side_effect = fake_savefig
            return fig, [ax]

        with patch("mplfinance.plot", side_effect=fake_plot):
            await _call_image(
                "render_candlestick",
                code="42880",
                from_date="2026-04-01",
                to_date="2026-04-06",
                indicators=["volume"],
            )

        assert captured.get("returnfig") is True
        assert "savefig" not in captured  # savefig must be omitted in this path

    async def test_lock_day_with_sma_overlay(self, mock_env):
        # SMA addplot + lock day must coexist on the returnfig path.
        # This guards against an mplfinance regression where addplot +
        # returnfig combos break, since the lock-day branch routes
        # through the returnfig API instead of savefig.
        rows = []
        for i in range(30):
            d = (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("42880", d, o=600, h=620, low=580, c=610))
        lock_day = _bar("42880", "2026-05-01", o=940, h=940, low=940, c=940)
        lock_day["AdjO"] = lock_day["AdjH"] = lock_day["AdjL"] = lock_day["AdjC"] = 940
        lock_day["UL"] = "1"
        lock_day["LL"] = "0"
        rows.append(lock_day)
        _seed(mock_env["cache"], rows)

        png = await _call_image(
            "render_candlestick",
            code="42880",
            from_date="2026-04-01",
            to_date="2026-05-01",
            indicators=["volume", "sma5"],
        )
        assert _is_real_chart_png(png)

    async def test_lock_day_uses_correct_color_per_direction(self, mock_env):
        # Pin the colour-by-direction logic: lock-high → up colour
        # (default style: 'g'); lock-low → down colour ('r'). Without
        # this, swapping the two would silently render symmetrically.
        rows = []
        for i in range(5):
            d = (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("27801", d, o=600, h=620, low=580, c=610))
        lock_high = _bar("27801", "2026-04-06", o=940, h=940, low=940, c=940)
        lock_high["AdjO"] = lock_high["AdjH"] = lock_high["AdjL"] = lock_high["AdjC"] = 940
        lock_high["UL"] = "1"
        lock_high["LL"] = "0"
        rows.append(lock_high)
        lock_low = _bar("27801", "2026-04-07", o=400, h=400, low=400, c=400)
        lock_low["AdjO"] = lock_low["AdjH"] = lock_low["AdjL"] = lock_low["AdjC"] = 400
        lock_low["UL"] = "0"
        lock_low["LL"] = "1"
        rows.append(lock_low)
        _seed(mock_env["cache"], rows)

        fig = MagicMock()
        ax = MagicMock()

        def fake_savefig(buf, **_kw):
            buf.write(_make_fake_png())

        fig.savefig.side_effect = fake_savefig

        def fake_plot(_df, **_kwargs):
            return fig, [ax]

        with patch("mplfinance.plot", side_effect=fake_plot):
            await _call_image(
                "render_candlestick",
                code="27801",
                from_date="2026-04-01",
                to_date="2026-04-07",
                indicators=["volume"],
                style="default",
            )

        # Two lock days → two hlines calls.
        assert ax.hlines.call_count == 2
        colors = [call.kwargs["colors"] for call in ax.hlines.call_args_list]
        # default style: up='g', down='r'
        assert "g" in colors  # lock-high used the up colour
        assert "r" in colors  # lock-low used the down colour

    async def test_no_lock_day_uses_savefig_path(self, mock_env):
        # Without any lock day, the existing savefig path must still run
        # (no regression in the normal case).
        rows = [
            _bar("27800", (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d"))
            for i in range(10)
        ]
        _seed(mock_env["cache"], rows)

        captured: dict = {}

        def fake_plot(df, **kwargs):
            captured.update(kwargs)
            kwargs["savefig"]["fname"].write(_make_fake_png())

        with patch("mplfinance.plot", side_effect=fake_plot):
            await _call_image(
                "render_candlestick",
                code="27800",
                from_date="2026-04-01",
                to_date="2026-04-10",
                indicators=["volume"],
            )

        # savefig path was taken; returnfig was not requested.
        assert "savefig" in captured
        assert captured.get("returnfig") is None or captured.get("returnfig") is False


class TestRenderComparisonChart:
    async def test_returns_png_two_stocks(self, mock_env):
        for code, base in [("27800", 100), ("72030", 200)]:
            rows = []
            for i in range(20):
                d = (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d")
                rows.append(_bar(code, d, c=float(base + i)))
            _seed(mock_env["cache"], rows)

        png = await _call_image(
            "render_comparison_chart",
            codes=["27800", "72030"],
            from_date="2026-01-05",
            to_date="2026-01-24",
        )
        assert _is_real_comparison_png(png)

    async def test_price_mode_renders(self, mock_env):
        rows = []
        for i in range(15):
            d = (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, c=100.0 + i))
        _seed(mock_env["cache"], rows)

        png = await _call_image(
            "render_comparison_chart",
            codes=["27800"],
            from_date="2026-01-05",
            to_date="2026-01-19",
            mode="price",
        )
        assert _is_real_comparison_png(png)

    async def test_colorblind_style_renders(self, mock_env):
        rows = []
        for i in range(15):
            d = (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, c=100.0 + i))
        _seed(mock_env["cache"], rows)

        png = await _call_image(
            "render_comparison_chart",
            codes=["27800"],
            from_date="2026-01-05",
            to_date="2026-01-19",
            style="colorblind",
        )
        assert _is_real_comparison_png(png)

    async def test_missing_stock_skipped_others_render(self, mock_env):
        # 99999 has no data — chart renders with the available series only.
        rows = []
        for i in range(15):
            d = (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, c=100.0 + i))
        _seed(mock_env["cache"], rows)

        png = await _call_image(
            "render_comparison_chart",
            codes=["27800", "99999"],
            from_date="2026-01-05",
            to_date="2026-01-19",
        )
        assert _is_real_comparison_png(png)

    async def test_return_pct_baseline_handles_late_ipo(self, mock_env):
        # Stock A: full window. Stock B: starts mid-window (IPO).
        # return_pct must use bfill so B normalises to 0 % at its own
        # first available bar rather than producing a NaN at df.iloc[0].
        rows_a = []
        for i in range(20):
            d = (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows_a.append(_bar("27800", d, c=100.0 + i))
        _seed(mock_env["cache"], rows_a)

        rows_b = []
        for i in range(10, 20):  # starts halfway through the window
            d = (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows_b.append(_bar("72030", d, c=200.0 + i))
        _seed(mock_env["cache"], rows_b)

        png = await _call_image(
            "render_comparison_chart",
            codes=["27800", "72030"],
            from_date="2026-01-05",
            to_date="2026-01-24",
        )
        assert _is_real_comparison_png(png)

    async def test_empty_codes_returns_error_image(self, mock_env):
        png = await _call_image(
            "render_comparison_chart",
            codes=[],
            from_date="2026-01-05",
            to_date="2026-01-10",
        )
        assert _is_error_image_png(png)

    async def test_too_many_codes_returns_error_image(self, mock_env):
        png = await _call_image(
            "render_comparison_chart",
            codes=[
                "1111",
                "2222",
                "3333",
                "4444",
                "5555",
                "6666",
                "7777",
                "8888",
                "9999",
                "1234",
                "5678",
            ],
            from_date="2026-01-05",
            to_date="2026-01-10",
        )
        assert _is_error_image_png(png)

    async def test_invalid_code_returns_error_image(self, mock_env):
        png = await _call_image(
            "render_comparison_chart",
            codes=["bad"],
            from_date="2026-01-05",
            to_date="2026-01-10",
        )
        assert _is_error_image_png(png)

    async def test_no_cached_data_returns_error_image(self, mock_env):
        png = await _call_image(
            "render_comparison_chart",
            codes=["99999"],
            from_date="2026-01-05",
            to_date="2026-01-10",
        )
        assert _is_error_image_png(png)

    async def test_inverted_dates_returns_error_image(self, mock_env):
        png = await _call_image(
            "render_comparison_chart",
            codes=["27800"],
            from_date="2026-02-01",
            to_date="2026-01-01",
        )
        assert _is_error_image_png(png)

    async def test_unknown_mode_returns_error_image(self, mock_env):
        png = await _call_image(
            "render_comparison_chart",
            codes=["27800"],
            from_date="2026-01-05",
            to_date="2026-01-10",
            mode="absolute",
        )
        assert _is_error_image_png(png)

    async def test_unknown_style_returns_error_image(self, mock_env):
        png = await _call_image(
            "render_comparison_chart",
            codes=["27800"],
            from_date="2026-01-05",
            to_date="2026-01-10",
            style="neon",
        )
        assert _is_error_image_png(png)

    async def test_misaligned_dates_renders_without_error(self, mock_env):
        # Regression: one stock missing a single trading day that others have
        # (e.g. 9984/7974 missing 2026-03-11 on m1.local cache) produces NaN
        # in the outer-joined DataFrame. Without ffill, matplotlib breaks the
        # line at the NaN. Verify the chart renders successfully.
        rows_a = []
        for i in range(5):
            d = (datetime(2026, 3, 9) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows_a.append(_bar("27800", d, c=100.0 + i))
        _seed(mock_env["cache"], rows_a)

        # Stock B intentionally missing 2026-03-11 (index position 2).
        rows_b = [
            _bar("72030", "2026-03-09", c=200.0),
            _bar("72030", "2026-03-10", c=201.0),
            # 2026-03-11 absent
            _bar("72030", "2026-03-12", c=203.0),
            _bar("72030", "2026-03-13", c=204.0),
        ]
        _seed(mock_env["cache"], rows_b)

        png = await _call_image(
            "render_comparison_chart",
            codes=["27800", "72030"],
            from_date="2026-03-09",
            to_date="2026-03-13",
        )
        assert _is_real_comparison_png(png)

    async def test_custom_labels_render(self, mock_env):
        rows = []
        for i in range(15):
            d = (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, c=100.0 + i))
        _seed(mock_env["cache"], rows)

        png = await _call_image(
            "render_comparison_chart",
            codes=["27800"],
            from_date="2026-01-05",
            to_date="2026-01-19",
            labels=["My Label"],
        )
        assert _is_real_comparison_png(png)

    async def test_labels_length_mismatch_returns_error(self, mock_env):
        rows = []
        for i in range(10):
            d = (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, c=100.0 + i))
        _seed(mock_env["cache"], rows)

        png = await _call_image(
            "render_comparison_chart",
            codes=["27800"],
            from_date="2026-01-05",
            to_date="2026-01-14",
            labels=["Label A", "Label B"],  # length mismatch
        )
        assert _is_error_image_png(png)

    async def test_empty_label_falls_back_to_auto(self, mock_env):
        rows = []
        for i in range(15):
            d = (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, c=100.0 + i))
        _seed(mock_env["cache"], rows)

        png = await _call_image(
            "render_comparison_chart",
            codes=["27800"],
            from_date="2026-01-05",
            to_date="2026-01-19",
            labels=[""],  # empty → auto-label
        )
        assert _is_real_comparison_png(png)

    async def test_whitespace_only_label_falls_back_to_auto(self, mock_env):
        rows = []
        for i in range(15):
            d = (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, c=100.0 + i))
        _seed(mock_env["cache"], rows)

        png = await _call_image(
            "render_comparison_chart",
            codes=["27800"],
            from_date="2026-01-05",
            to_date="2026-01-19",
            labels=["   "],  # whitespace-only → auto-label
        )
        assert _is_real_comparison_png(png)

    async def test_landscape_aspect_ratio_renders(self, mock_env):
        rows = []
        for i in range(15):
            d = (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, c=100.0 + i))
        _seed(mock_env["cache"], rows)

        png = await _call_image(
            "render_comparison_chart",
            codes=["27800"],
            from_date="2026-01-05",
            to_date="2026-01-19",
            aspect_ratio="landscape",
        )
        assert _is_real_comparison_png(png)

    async def test_portrait_aspect_ratio_renders(self, mock_env):
        rows = []
        for i in range(15):
            d = (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, c=100.0 + i))
        _seed(mock_env["cache"], rows)

        png = await _call_image(
            "render_comparison_chart",
            codes=["27800"],
            from_date="2026-01-05",
            to_date="2026-01-19",
            aspect_ratio="portrait",
        )
        assert _is_real_comparison_png(png)

    async def test_unknown_aspect_ratio_returns_error(self, mock_env):
        png = await _call_image(
            "render_comparison_chart",
            codes=["27800"],
            from_date="2026-01-05",
            to_date="2026-01-10",
            aspect_ratio="widescreen",
        )
        assert _is_error_image_png(png)


def test_brief_company_name():
    from jquants_mcp.tools.charts import _brief_company_name

    # Single-segment name with 株式会社: no U+3000 separator, so the corp suffix
    # is NOT stripped (it's the entire name, not a management company prefix).
    assert _brief_company_name("ＡＢＣ株式会社") == "ABC株式会社"

    # Regular stock: no ideographic space, parenthetical kept as-is.
    # (Real J-Quants CoNames for ordinary stocks do not carry （普通株）;
    # this is a synthetic edge case to verify no crash.)
    assert _brief_company_name("トヨタ自動車（普通株）") == "トヨタ自動車(普通株)"

    # Real ETF (code 13050): management company prefix stripped via U+3000 split,
    # iFreeETF → iFree (ETF suffix removed), parenthetical kept.
    # Distinguishes from 日経225 variant below.
    assert (
        _brief_company_name(
            "大和アセットマネジメント株式会社　ｉＦｒｅｅＥＴＦ　ＴＯＰＩＸ（年１回決算型）"
        )
        == "iFree TOPIX(年1回決算型)"
    )

    # Real ETF (code 13200): same brand/type, different index — must produce a
    # DIFFERENT label from the TOPIX variant above.
    assert (
        _brief_company_name(
            "大和アセットマネジメント株式会社　ｉＦｒｅｅＥＴＦ　日経２２５（年１回決算型）"
        )
        == "iFree 日経225(年1回決算型)"
    )

    # Multi-segment company name (Global　X　Japan株式会社): corp suffix found
    # at part index 2, all three leading parts dropped.
    assert _brief_company_name(
        "Ｇｌｏｂａｌ　Ｘ　Ｊａｐａｎ株式会社　グローバルＸ　ＵＳ　ＲＥＩＴ・トップ２０　ＥＴＦ（隔月分配型）"
    ).startswith("グローバルX")

    # Truncation: result must be <= 20 chars and end with ellipsis.
    long_result = _brief_company_name(
        "野村アセットマネジメント株式会社　ＮＥＸＴ　ＦＵＮＤＳ　"
        "新興国債券・Ｊ．Ｐ．モルガン・エマージング・マーケット・ボンド・インデックス・プラス（為替ヘッジなし）連動型上場投信"
    )
    assert len(long_result) <= 20
    assert long_result.endswith("…")

    # Empty input passthrough.
    assert _brief_company_name("") == ""


def test_register_no_op_when_extras_missing():
    """register() should silently skip when mplfinance isn't importable.

    Simulated by patching the import to raise. Confirms the lean stdio
    profile (no [charts] extras) starts cleanly.
    """
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
