"""Tests for chart-rendering tools."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

import jquants_mcp.server as server_module
from jquants_mcp.cache.store import CacheStore
from jquants_mcp.client import JQuantsClient
from jquants_mcp.config import Settings


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


async def _call(tool: str, **kwargs) -> dict:
    """Invoke a JSON-returning tool and return the parsed dict."""
    result = await server_module.mcp.call_tool(tool, kwargs)
    return json.loads(result.content[0].text)


def _seed_master(cache: CacheStore, code: str, name: str | None, date: str = "2026-01-04") -> None:
    """Seed an ``equities_master`` row so ``_get_company_name`` can find it.

    J-Quants API v2 uses the short-form key ``CoName`` (not the longer
    ``CompanyName`` shown in some doc pages); the cache stores the
    response verbatim so the seed must use the same key.
    """
    row = {"Code": code, "Date": date, "CoName": name}
    cache.put_rows("equities_master", [row], key_columns=["Code", "Date"])


class TestCompanyNameHelpers:
    """Unit tests for ``_get_company_name``."""

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


class TestGetComparisonChartData:
    async def test_returns_json_two_stocks(self, mock_env):
        for code, base in [("27800", 100), ("72030", 200)]:
            rows = []
            for i in range(5):
                d = (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d")
                rows.append(_bar(code, d, c=float(base + i)))
            _seed(mock_env["cache"], rows)

        result = await _call(
            "get_comparison_chart_data",
            codes=["27800", "72030"],
            from_date="2026-01-05",
            to_date="2026-01-09",
        )
        assert "error" not in result
        assert result["mode"] == "return_pct"
        assert result["from_date"] == "2026-01-05"
        assert result["to_date"] == "2026-01-09"
        assert len(result["series_keys"]) == 2
        assert len(result["records"]) == 5
        assert result["records"][0]["date"] == "2026-01-05"

    async def test_return_pct_normalises_to_zero(self, mock_env):
        rows = []
        for i in range(5):
            d = (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, c=100.0 + i * 10))
        _seed(mock_env["cache"], rows)

        result = await _call(
            "get_comparison_chart_data",
            codes=["27800"],
            from_date="2026-01-05",
            to_date="2026-01-09",
            mode="return_pct",
        )
        assert "error" not in result
        label = result["series_keys"][0]
        # First record must be 0.0 (baseline)
        assert result["records"][0][label] == pytest.approx(0.0)
        # Last record must reflect the cumulative return
        assert result["records"][-1][label] == pytest.approx(40.0)

    async def test_price_mode_returns_raw_prices(self, mock_env):
        rows = []
        for i in range(5):
            d = (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, c=100.0 + i))
        _seed(mock_env["cache"], rows)

        result = await _call(
            "get_comparison_chart_data",
            codes=["27800"],
            from_date="2026-01-05",
            to_date="2026-01-09",
            mode="price",
        )
        assert "error" not in result
        assert result["mode"] == "price"
        label = result["series_keys"][0]
        assert result["records"][0][label] == pytest.approx(100.0)
        assert result["records"][-1][label] == pytest.approx(104.0)

    async def test_missing_stock_skipped_others_returned(self, mock_env):
        # 99999 has no data — result contains the available series only.
        rows = []
        for i in range(5):
            d = (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, c=100.0 + i))
        _seed(mock_env["cache"], rows)

        result = await _call(
            "get_comparison_chart_data",
            codes=["27800", "99999"],
            from_date="2026-01-05",
            to_date="2026-01-09",
        )
        assert "error" not in result
        assert len(result["series_keys"]) == 1

    async def test_return_pct_baseline_handles_late_ipo(self, mock_env):
        # Stock A: full window. Stock B: starts mid-window (late IPO).
        # return_pct must bfill so B normalises to 0% at its own first bar.
        rows_a = [
            _bar(
                "27800",
                (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d"),
                c=100.0 + i,
            )
            for i in range(5)
        ]
        _seed(mock_env["cache"], rows_a)

        rows_b = [
            _bar(
                "72030",
                (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d"),
                c=200.0 + i,
            )
            for i in range(3, 5)  # starts at 2026-01-08
        ]
        _seed(mock_env["cache"], rows_b)

        result = await _call(
            "get_comparison_chart_data",
            codes=["27800", "72030"],
            from_date="2026-01-05",
            to_date="2026-01-09",
        )
        assert "error" not in result
        assert len(result["series_keys"]) == 2

    async def test_ffill_missing_day(self, mock_env):
        # Stock B missing 2026-01-07 — ffill carries 2026-01-06 value forward.
        rows_a = [
            _bar("27800", "2026-01-05", c=100.0),
            _bar("27800", "2026-01-06", c=101.0),
            _bar("27800", "2026-01-07", c=102.0),
        ]
        _seed(mock_env["cache"], rows_a)

        rows_b = [
            _bar("72030", "2026-01-05", c=200.0),
            _bar("72030", "2026-01-06", c=202.0),
            # 2026-01-07 absent
        ]
        _seed(mock_env["cache"], rows_b)

        result = await _call(
            "get_comparison_chart_data",
            codes=["27800", "72030"],
            from_date="2026-01-05",
            to_date="2026-01-07",
            mode="price",
        )
        assert "error" not in result
        label_b = [k for k in result["series_keys"] if "7203" in k][0]
        # 2026-01-07 row must carry the 2026-01-06 value forward
        row_07 = next(r for r in result["records"] if r["date"] == "2026-01-07")
        assert row_07[label_b] == pytest.approx(202.0)

    async def test_records_date_format(self, mock_env):
        rows = [_bar("27800", "2026-01-05", c=100.0)]
        _seed(mock_env["cache"], rows)

        result = await _call(
            "get_comparison_chart_data",
            codes=["27800"],
            from_date="20260105",  # compact format
            to_date="20260105",
        )
        assert "error" not in result
        assert result["records"][0]["date"] == "2026-01-05"
        assert result["from_date"] == "2026-01-05"
        assert result["to_date"] == "2026-01-05"

    async def test_empty_codes_returns_error(self, mock_env):
        result = await _call(
            "get_comparison_chart_data",
            codes=[],
            from_date="2026-01-05",
            to_date="2026-01-10",
        )
        assert "error" in result

    async def test_too_many_codes_returns_error(self, mock_env):
        result = await _call(
            "get_comparison_chart_data",
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
        assert "error" in result

    async def test_invalid_code_returns_error(self, mock_env):
        result = await _call(
            "get_comparison_chart_data",
            codes=["bad"],
            from_date="2026-01-05",
            to_date="2026-01-10",
        )
        assert "error" in result

    async def test_no_cached_data_returns_error(self, mock_env):
        result = await _call(
            "get_comparison_chart_data",
            codes=["99999"],
            from_date="2026-01-05",
            to_date="2026-01-10",
        )
        assert "error" in result

    async def test_inverted_dates_returns_error(self, mock_env):
        result = await _call(
            "get_comparison_chart_data",
            codes=["27800"],
            from_date="2026-02-01",
            to_date="2026-01-01",
        )
        assert "error" in result

    async def test_unknown_mode_returns_error(self, mock_env):
        result = await _call(
            "get_comparison_chart_data",
            codes=["27800"],
            from_date="2026-01-05",
            to_date="2026-01-10",
            mode="absolute",
        )
        assert "error" in result

    async def test_custom_labels(self, mock_env):
        rows = [
            _bar(
                "27800",
                (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d"),
                c=100.0 + i,
            )
            for i in range(5)
        ]
        _seed(mock_env["cache"], rows)

        result = await _call(
            "get_comparison_chart_data",
            codes=["27800"],
            from_date="2026-01-05",
            to_date="2026-01-09",
            labels=["My Stock"],
        )
        assert "error" not in result
        assert result["series_keys"] == ["My Stock"]
        assert "My Stock" in result["records"][0]

    async def test_labels_length_mismatch_returns_error(self, mock_env):
        rows = [_bar("27800", "2026-01-05", c=100.0)]
        _seed(mock_env["cache"], rows)

        result = await _call(
            "get_comparison_chart_data",
            codes=["27800"],
            from_date="2026-01-05",
            to_date="2026-01-05",
            labels=["Label A", "Label B"],  # length mismatch
        )
        assert "error" in result

    async def test_empty_label_falls_back_to_auto(self, mock_env):
        rows = [
            _bar(
                "27800",
                (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d"),
                c=100.0 + i,
            )
            for i in range(5)
        ]
        _seed(mock_env["cache"], rows)

        result = await _call(
            "get_comparison_chart_data",
            codes=["27800"],
            from_date="2026-01-05",
            to_date="2026-01-09",
            labels=[""],  # empty → auto-label
        )
        assert "error" not in result
        # Auto-label contains the code display form
        assert any("2780" in k for k in result["series_keys"])

    async def test_whitespace_only_label_falls_back_to_auto(self, mock_env):
        rows = [
            _bar(
                "27800",
                (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d"),
                c=100.0 + i,
            )
            for i in range(5)
        ]
        _seed(mock_env["cache"], rows)

        result = await _call(
            "get_comparison_chart_data",
            codes=["27800"],
            from_date="2026-01-05",
            to_date="2026-01-09",
            labels=["   "],  # whitespace-only → auto-label
        )
        assert "error" not in result
        assert any("2780" in k for k in result["series_keys"])


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


def _seed_earnings(cache: CacheStore, code: str, dates: list[str]) -> None:
    """Insert rows into equities_earnings_calendar for testing."""
    # Access _db_path directly: CacheStore exposes no public "insert raw row" API
    # and opening a second connection to the same file is safe with WAL mode.
    conn = sqlite3.connect(str(cache._db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS equities_earnings_calendar "
        "(code TEXT NOT NULL, date TEXT NOT NULL, data TEXT NOT NULL, "
        "fetched_at REAL NOT NULL, PRIMARY KEY (code, date))"
    )
    now = time.time()
    for d in dates:
        conn.execute(
            "INSERT OR REPLACE INTO equities_earnings_calendar "
            "(code, date, data, fetched_at) VALUES (?, ?, ?, ?)",
            (code, d, json.dumps({"Code": code, "Date": d}), now),
        )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
class TestGetCandlestickData:
    async def test_basic_structure(self, mock_env):
        """Returns expected top-level keys for a normal request."""
        rows = [_bar("72030", f"2026-01-0{i + 1}", c=float(100 + i)) for i in range(5)]
        _seed(mock_env["cache"], rows)

        result = await _call(
            "get_candlestick_data",
            code="7203",
            from_date="2026-01-01",
            to_date="2026-01-05",
            indicators=[],
        )
        assert "error" not in result
        assert result["code"] == "72030"
        assert result["display_code"] == "7203"
        assert result["from_date"] == "2026-01-01"
        assert result["to_date"] == "2026-01-05"
        assert result["adjusted"] is True
        assert len(result["dates"]) == 5
        assert result["dates"][0] == "2026-01-01"
        for key in ("open", "high", "low", "close", "volume"):
            assert key in result["ohlcv"]
            assert len(result["ohlcv"][key]) == 5
        assert isinstance(result["lock_days"], list)
        assert isinstance(result["earnings_dates"], list)

    async def test_ohlcv_values_match_input(self, mock_env):
        """OHLCV parallel arrays carry the adjusted values from the cache."""
        rows = [_bar("72030", "2026-01-05", o=100, h=110, low=90, c=105, vo=2000)]
        _seed(mock_env["cache"], rows)

        result = await _call(
            "get_candlestick_data",
            code="7203",
            from_date="2026-01-05",
            to_date="2026-01-05",
            indicators=[],
        )
        assert result["ohlcv"]["open"] == [100.0]
        assert result["ohlcv"]["high"] == [110.0]
        assert result["ohlcv"]["low"] == [90.0]
        assert result["ohlcv"]["close"] == [105.0]
        assert result["ohlcv"]["volume"] == [2000.0]

    async def test_default_indicators_include_sma5_sma25(self, mock_env):
        """Default indicators list is ['volume','sma5','sma25']."""
        rows = [_bar("72030", f"2026-01-{i + 1:02d}") for i in range(10)]
        _seed(mock_env["cache"], rows)

        result = await _call(
            "get_candlestick_data",
            code="7203",
            from_date="2026-01-01",
            to_date="2026-01-10",
        )
        assert "error" not in result
        # volume is in ohlcv, not indicators
        assert "volume" not in result["indicators"]
        assert "sma5" in result["indicators"]
        assert "sma25" in result["indicators"]

    async def test_sma5_computation(self, mock_env):
        """SMA5 is None for the first 4 bars and equals the rolling mean for bars 5+."""
        closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0]
        rows = [_bar("72030", f"2026-01-0{i + 1}", c=closes[i]) for i in range(7)]
        _seed(mock_env["cache"], rows)

        result = await _call(
            "get_candlestick_data",
            code="7203",
            from_date="2026-01-01",
            to_date="2026-01-07",
            indicators=["sma5"],
        )
        assert "error" not in result
        sma5 = result["indicators"]["sma5"]
        assert sma5[0] is None
        assert sma5[1] is None
        assert sma5[2] is None
        assert sma5[3] is None
        assert sma5[4] == pytest.approx(sum(closes[:5]) / 5)
        assert sma5[5] == pytest.approx(sum(closes[1:6]) / 5)
        assert sma5[6] == pytest.approx(sum(closes[2:7]) / 5)

    async def test_bb20_returns_three_series(self, mock_env):
        """bb20 indicator expands into bb20_upper, bb20_mid, bb20_lower."""
        rows = [
            _bar(
                "72030",
                (datetime(2026, 1, 1) + timedelta(days=i)).strftime("%Y-%m-%d"),
                c=float(100 + i),
            )
            for i in range(25)
        ]
        _seed(mock_env["cache"], rows)

        result = await _call(
            "get_candlestick_data",
            code="7203",
            from_date="2026-01-01",
            to_date="2026-01-25",
            indicators=["bb20"],
        )
        assert "error" not in result
        assert "bb20_upper" in result["indicators"]
        assert "bb20_mid" in result["indicators"]
        assert "bb20_lower" in result["indicators"]
        assert "bb20" not in result["indicators"]
        # First 19 values are None (warm-up), 20th onward are valid
        assert result["indicators"]["bb20_mid"][18] is None
        assert result["indicators"]["bb20_mid"][19] is not None
        # upper > mid > lower
        assert result["indicators"]["bb20_upper"][19] > result["indicators"]["bb20_mid"][19]
        assert result["indicators"]["bb20_mid"][19] > result["indicators"]["bb20_lower"][19]

    async def test_unknown_indicator_returns_error(self, mock_env):
        result = await _call("get_candlestick_data", code="7203", indicators=["sma999"])
        assert "error" in result
        assert "sma999" in result["error"]

    async def test_invalid_code_returns_error(self, mock_env):
        result = await _call("get_candlestick_data", code="INVALID")
        assert "error" in result

    async def test_invalid_date_returns_error(self, mock_env):
        result = await _call("get_candlestick_data", code="7203", from_date="not-a-date")
        assert "error" in result

    async def test_from_gt_to_returns_error(self, mock_env):
        result = await _call(
            "get_candlestick_data",
            code="7203",
            from_date="2026-02-01",
            to_date="2026-01-01",
            indicators=[],
        )
        assert "error" in result

    async def test_no_cached_bars_returns_error(self, mock_env):
        result = await _call(
            "get_candlestick_data",
            code="7203",
            from_date="2026-01-01",
            to_date="2026-01-05",
            indicators=[],
        )
        assert "error" in result
        assert "No cached bars" in result["error"]

    async def test_adjusted_false_uses_raw_prices(self, mock_env):
        """adjusted=False reads O/H/L/C/Vo instead of AdjO/AdjH/AdjL/AdjC/AdjVo."""
        rows = [_bar("72030", "2026-01-05", o=200, h=220, low=180, c=210, vo=500, adj_factor=0.5)]
        # Override raw vs adjusted values so they differ
        rows[0]["O"] = 400.0
        rows[0]["C"] = 420.0
        _seed(mock_env["cache"], rows)

        result = await _call(
            "get_candlestick_data",
            code="7203",
            from_date="2026-01-05",
            to_date="2026-01-05",
            indicators=[],
            adjusted=False,
        )
        assert "error" not in result
        assert result["adjusted"] is False
        assert result["ohlcv"]["open"] == [400.0]
        assert result["ohlcv"]["close"] == [420.0]

    async def test_lock_day_detected(self, mock_env):
        """A 寄らずストップ高 bar appears in lock_days."""
        row = _bar("72030", "2026-01-05", o=100, h=100, low=100, c=100, vo=0)
        row["UL"] = "1"
        _seed(mock_env["cache"], [row])

        result = await _call(
            "get_candlestick_data",
            code="7203",
            from_date="2026-01-05",
            to_date="2026-01-05",
            indicators=[],
        )
        assert "error" not in result
        assert len(result["lock_days"]) == 1
        assert result["lock_days"][0]["direction"] == "high"
        assert result["lock_days"][0]["price"] == pytest.approx(100.0)

    async def test_earnings_dates_populated(self, mock_env):
        """earnings_dates includes dates seeded in equities_earnings_calendar."""
        rows = [_bar("72030", "2026-01-05")]
        _seed(mock_env["cache"], rows)
        _seed_earnings(mock_env["cache"], "72030", ["2026-01-05"])

        result = await _call(
            "get_candlestick_data",
            code="7203",
            from_date="2026-01-05",
            to_date="2026-01-05",
            indicators=[],
        )
        assert "error" not in result
        assert result["earnings_dates"] == ["2026-01-05"]
