"""Tests for scripts/daily_fetch.py.

daily_fetch.py does not depend on pandas at runtime; tests use a
lightweight DataFrame-compatible mock to simulate API responses.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import sys

# daily_fetch.py は jquantsapi に依存するが、jquants-mcp の .venv には入っていない。
# テスト用に mock モジュールを差し込む。
_jquantsapi_mock = MagicMock()
sys.modules.setdefault("jquantsapi", _jquantsapi_mock)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from daily_fetch import (  # noqa: E402
    ENDPOINT_MIN_PLAN,
    FETCH_REGISTRY,
    TTL_6H,
    TTL_24H,
    TTL_90D,
    _available_endpoints,
    _ensure_tables,
    _fetch_markets_tier1,
    _sanitize_row,
    _store_response_cache,
    _store_tier1,
    fetch_earnings_calendar,
    fetch_fins_summary,
    fetch_investor_types,
    fetch_short_ratio,
    fetch_short_sale_report,
    fetch_topix,
)


# ============================================================
# DataFrame 互換の軽量 mock
# ============================================================


class FakeRow:
    """DataFrame の行を模倣する dict ラッパー。"""

    def __init__(self, data: dict):
        self._data = data

    def to_dict(self) -> dict:
        return dict(self._data)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getitem__(self, key):
        return self._data[key]


class FakeDataFrame:
    """pandas.DataFrame の最小互換 mock。iterrows() と len() をサポート。"""

    def __init__(self, rows: list[dict] | None = None):
        self._rows = rows or []

    def __len__(self):
        return len(self._rows)

    def iterrows(self):
        for i, row in enumerate(self._rows):
            yield i, FakeRow(row)


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture()
def db_conn(tmp_path: Path):
    """テスト用 SQLite 接続を提供する。"""
    db_path = tmp_path / "test_cache.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_tables(conn)
    yield conn
    conn.close()


def _mock_cli(**method_returns) -> MagicMock:
    """jquantsapi.ClientV2 の mock を生成する。"""
    cli = MagicMock()
    for method_name, return_value in method_returns.items():
        getattr(cli, method_name).return_value = return_value
    return cli


# ============================================================
# プラン判定
# ============================================================


class TestAvailableEndpoints:
    """_available_endpoints のテスト。"""

    def test_free_plan(self):
        eps = _available_endpoints("free")
        assert "fins_summary" in eps
        assert "earnings_cal" in eps
        assert "topix" not in eps
        assert "short_ratio" not in eps

    def test_light_plan(self):
        eps = _available_endpoints("light")
        assert "fins_summary" in eps
        assert "earnings_cal" in eps
        assert "topix" in eps
        assert "investor_types" in eps
        assert "short_ratio" not in eps
        assert "breakdown" not in eps

    def test_standard_plan(self):
        eps = _available_endpoints("standard")
        assert "short_ratio" in eps
        assert "margin_interest" in eps
        assert "margin_alert" in eps
        assert "short_sale_report" in eps
        assert "breakdown" not in eps

    def test_premium_plan(self):
        eps = _available_endpoints("premium")
        assert "breakdown" in eps
        # 全エンドポイントが含まれる
        assert set(eps) == set(ENDPOINT_MIN_PLAN.keys())

    def test_unknown_plan_defaults_to_free(self):
        eps = _available_endpoints("unknown")
        assert eps == _available_endpoints("free")

    def test_registry_covers_all_endpoints(self):
        """FETCH_REGISTRY が ENDPOINT_MIN_PLAN の全エンドポイントをカバーしていること。"""
        assert set(FETCH_REGISTRY.keys()) == set(ENDPOINT_MIN_PLAN.keys())


# ============================================================
# ユーティリティ
# ============================================================


class TestSanitizeRow:
    """_sanitize_row のテスト。"""

    def test_nan_to_none(self):
        result = _sanitize_row({"a": 1, "b": float("nan"), "c": "text"})
        assert result == {"a": 1, "b": None, "c": "text"}

    def test_normal_float_preserved(self):
        result = _sanitize_row({"x": 3.14})
        assert result == {"x": 3.14}


# ============================================================
# Tier 2 キャッシュヘルパー
# ============================================================


class TestStoreResponseCache:
    """_store_response_cache のテスト。"""

    def test_basic_store(self, db_conn):
        records = [{"a": 1}, {"a": 2}]
        n = _store_response_cache(db_conn, "/test/endpoint", records, TTL_24H)
        assert n == 2

        row = db_conn.execute(
            "SELECT data, ttl_seconds FROM response_cache WHERE cache_key=?",
            ("/test/endpoint",),
        ).fetchone()
        assert row is not None
        assert json.loads(row[0]) == records
        assert row[1] == TTL_24H

    def test_empty_records(self, db_conn):
        n = _store_response_cache(db_conn, "/test/empty", [], TTL_6H)
        assert n == 0

    def test_upsert(self, db_conn):
        """同じキーで上書きされること。"""
        _store_response_cache(db_conn, "/test/key", [{"v": 1}], TTL_6H)
        _store_response_cache(db_conn, "/test/key", [{"v": 2}], TTL_24H)

        row = db_conn.execute(
            "SELECT data, ttl_seconds FROM response_cache WHERE cache_key=?",
            ("/test/key",),
        ).fetchone()
        assert json.loads(row[0]) == [{"v": 2}]
        assert row[1] == TTL_24H


# ============================================================
# TOPIX
# ============================================================


class TestFetchTopix:
    """fetch_topix のテスト。"""

    def test_empty_cache_full_fetch(self, db_conn):
        """キャッシュ空 → 全期間取得。"""
        df = FakeDataFrame(
            [
                {"Date": "2026-02-24", "O": 2700, "C": 2750},
                {"Date": "2026-02-25", "O": 2750, "C": 2800},
            ]
        )
        cli = _mock_cli(get_idx_bars_daily_topix=df)

        n = fetch_topix(cli, db_conn, "light")
        assert n == 2
        cli.get_idx_bars_daily_topix.assert_called_once_with()

        count = db_conn.execute("SELECT COUNT(*) FROM indices_bars_daily_topix").fetchone()[0]
        assert count == 2

    def test_incremental_fetch(self, db_conn):
        """キャッシュあり → from_date 指定で差分取得。"""
        db_conn.execute(
            "INSERT INTO indices_bars_daily_topix (date, data, fetched_at) VALUES (?, ?, ?)",
            ("2026-02-24", '{"Date":"2026-02-24"}', 1.0),
        )
        db_conn.commit()

        df = FakeDataFrame([{"Date": "2026-02-25", "O": 2750, "C": 2800}])
        cli = _mock_cli(get_idx_bars_daily_topix=df)

        n = fetch_topix(cli, db_conn, "light")
        assert n == 1
        cli.get_idx_bars_daily_topix.assert_called_once_with(from_yyyymmdd="20260225")

    def test_timestamp_in_date(self, db_conn):
        """日付に時刻部分が含まれる場合でも正常動作すること。"""
        db_conn.execute(
            "INSERT INTO indices_bars_daily_topix (date, data, fetched_at) VALUES (?, ?, ?)",
            ("2026-02-24 00:00:00", '{"Date":"2026-02-24 00:00:00"}', 1.0),
        )
        db_conn.commit()

        cli = _mock_cli(get_idx_bars_daily_topix=FakeDataFrame())
        n = fetch_topix(cli, db_conn, "light")
        assert n == 0
        cli.get_idx_bars_daily_topix.assert_called_once_with(from_yyyymmdd="20260225")

    def test_empty_response(self, db_conn):
        cli = _mock_cli(get_idx_bars_daily_topix=FakeDataFrame())
        n = fetch_topix(cli, db_conn, "light")
        assert n == 0


# ============================================================
# 決算サマリー
# ============================================================


class TestFetchFinsSummary:
    """fetch_fins_summary のテスト。"""

    def test_basic_fetch(self, db_conn):
        df = FakeDataFrame(
            [
                {"Code": "72030", "DiscDate": "2026-02-25", "Revenue": 1000000},
                {"Code": "99830", "DiscDate": "2026-02-25", "Revenue": 2000000},
            ]
        )
        cli = MagicMock()
        cli.get_fin_summary.side_effect = [df] + [FakeDataFrame()] * 6

        n = fetch_fins_summary(cli, db_conn, "light")
        assert n == 2

        count = db_conn.execute("SELECT COUNT(*) FROM fins_summary").fetchone()[0]
        assert count == 2

    def test_api_error_skips(self, db_conn):
        """API エラー時はスキップして続行。"""
        cli = MagicMock()
        cli.get_fin_summary.side_effect = Exception("API Error")

        n = fetch_fins_summary(cli, db_conn, "light")
        assert n == 0


# ============================================================
# 決算発表予定
# ============================================================


class TestFetchEarningsCalendar:
    """fetch_earnings_calendar のテスト。"""

    def test_basic_fetch(self, db_conn):
        df = FakeDataFrame(
            [
                {"Code": "72030", "Date": "2026-03-01"},
                {"Code": "99830", "Date": "2026-03-01"},
            ]
        )
        cli = _mock_cli(get_eq_earnings_cal=df)

        n = fetch_earnings_calendar(cli, db_conn, "light")
        assert n == 2

        # パラメータなしキー（最新データ用）
        row = db_conn.execute(
            "SELECT data, ttl_seconds FROM response_cache WHERE cache_key=?",
            ("/equities/earnings-calendar",),
        ).fetchone()
        assert row is not None
        assert row[1] == TTL_90D

        # 日付別キー（蓄積用）
        row_dated = db_conn.execute(
            "SELECT data, ttl_seconds FROM response_cache WHERE cache_key=?",
            ("/equities/earnings-calendar?date=20260301",),
        ).fetchone()
        assert row_dated is not None
        assert row_dated[1] == TTL_90D

    def test_tier1_rows_written(self, db_conn):
        """Tier 1 equities_earnings_calendar rows are written alongside Tier 2."""
        df = FakeDataFrame(
            [
                {"Code": "72030", "Date": "2026-03-01", "FQ": "3Q"},
                {"Code": "99830", "Date": "2026-03-01", "FQ": "1Q"},
            ]
        )
        cli = _mock_cli(get_eq_earnings_cal=df)
        fetch_earnings_calendar(cli, db_conn, "light")

        rows = db_conn.execute(
            "SELECT code, date FROM equities_earnings_calendar ORDER BY code"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][0] == "72030"  # code column
        assert rows[0][1] == "2026-03-01"  # date column
        assert rows[1][0] == "99830"

    def test_tier1_data_json_stored(self, db_conn):
        """Each Tier 1 row stores the full record as JSON in data column."""
        import json

        df = FakeDataFrame([{"Code": "72030", "Date": "2026-03-01", "FQ": "3Q"}])
        cli = _mock_cli(get_eq_earnings_cal=df)
        fetch_earnings_calendar(cli, db_conn, "light")

        row = db_conn.execute(
            "SELECT data FROM equities_earnings_calendar WHERE code=? AND date=?",
            ("72030", "2026-03-01"),
        ).fetchone()
        assert row is not None
        rec = json.loads(row[0])  # data column
        assert rec["FQ"] == "3Q"

    def test_empty_response(self, db_conn):
        cli = _mock_cli(get_eq_earnings_cal=FakeDataFrame())
        n = fetch_earnings_calendar(cli, db_conn, "light")
        assert n == 0


# ============================================================
# 投資部門別売買動向
# ============================================================


class TestFetchInvestorTypes:
    """fetch_investor_types のテスト。"""

    def test_basic_fetch(self, db_conn):
        df = FakeDataFrame(
            [
                {"PubDate": "2026-02-20", "Section": "TSEPrime", "FrgnBuy": 100},
                {"PubDate": "2026-02-20", "Section": "TSEStandard", "FrgnBuy": 50},
            ]
        )
        cli = _mock_cli(get_eq_investor_types=df)

        n = fetch_investor_types(cli, db_conn, "light")
        assert n == 2

        count = db_conn.execute("SELECT COUNT(*) FROM investor_types").fetchone()[0]
        assert count == 2

    def test_upsert(self, db_conn):
        """同じ pub_date+section で上書きされること。"""
        df1 = FakeDataFrame(
            [
                {"PubDate": "2026-02-20", "Section": "TSEPrime", "FrgnBuy": 100},
            ]
        )
        df2 = FakeDataFrame(
            [
                {"PubDate": "2026-02-20", "Section": "TSEPrime", "FrgnBuy": 999},
            ]
        )

        cli = _mock_cli(get_eq_investor_types=df1)
        fetch_investor_types(cli, db_conn, "light")

        cli = _mock_cli(get_eq_investor_types=df2)
        fetch_investor_types(cli, db_conn, "light")

        count = db_conn.execute("SELECT COUNT(*) FROM investor_types").fetchone()[0]
        assert count == 1

        row = db_conn.execute("SELECT data FROM investor_types").fetchone()
        data = json.loads(row[0])
        assert data["FrgnBuy"] == 999

    def test_empty_response(self, db_conn):
        cli = _mock_cli(get_eq_investor_types=FakeDataFrame())
        n = fetch_investor_types(cli, db_conn, "light")
        assert n == 0


# ============================================================
# _store_tier1（Tier 1 汎用ヘルパー）
# ============================================================


class TestStoreTier1:
    """_store_tier1 のテスト。"""

    def test_basic_store(self, db_conn):
        rows = [
            {"S33": "0050", "Date": "2026-03-10", "Ratio": 0.35},
            {"S33": "0050", "Date": "2026-03-11", "Ratio": 0.40},
        ]
        n = _store_tier1(
            db_conn,
            "markets_short_ratio",
            rows,
            key_mapping=[("S33", "s33"), ("Date", "date")],
        )
        assert n == 2

        count = db_conn.execute("SELECT COUNT(*) FROM markets_short_ratio").fetchone()[0]
        assert count == 2

    def test_upsert(self, db_conn):
        rows1 = [{"Code": "72030", "Date": "2026-03-10", "LoanBalance": 100}]
        rows2 = [{"Code": "72030", "Date": "2026-03-10", "LoanBalance": 999}]

        _store_tier1(
            db_conn,
            "markets_margin_interest",
            rows1,
            [("Code", "code"), ("Date", "date")],
        )
        _store_tier1(
            db_conn,
            "markets_margin_interest",
            rows2,
            [("Code", "code"), ("Date", "date")],
        )

        count = db_conn.execute("SELECT COUNT(*) FROM markets_margin_interest").fetchone()[0]
        assert count == 1

        row = db_conn.execute("SELECT data FROM markets_margin_interest").fetchone()
        data = json.loads(row[0])
        assert data["LoanBalance"] == 999

    def test_empty_rows(self, db_conn):
        n = _store_tier1(db_conn, "markets_calendar", [], [("Date", "date")])
        assert n == 0


# ============================================================
# _fetch_markets_tier1（Markets Tier 1 取得ロジック）
# ============================================================


class TestFetchMarketsTier1:
    """_fetch_markets_tier1 のテスト。"""

    def test_basic_fetch(self, db_conn):
        df = FakeDataFrame([{"S33": "0050", "Date": "2026-03-10", "Ratio": 0.35}])
        method = MagicMock(return_value=df)

        n = _fetch_markets_tier1(
            method,
            db_conn,
            table="markets_short_ratio",
            key_mapping=[("S33", "s33"), ("Date", "date")],
            plan="standard",
        )
        assert n == 1

        count = db_conn.execute("SELECT COUNT(*) FROM markets_short_ratio").fetchone()[0]
        assert count == 1

    def test_incremental_fetch(self, db_conn):
        """キャッシュあり → 差分取得。"""
        _store_tier1(
            db_conn,
            "markets_short_ratio",
            [{"S33": "0050", "Date": "2026-03-09", "Ratio": 0.30}],
            [("S33", "s33"), ("Date", "date")],
        )

        df = FakeDataFrame([{"S33": "0050", "Date": "2026-03-10", "Ratio": 0.35}])
        method = MagicMock(return_value=df)

        n = _fetch_markets_tier1(
            method,
            db_conn,
            table="markets_short_ratio",
            key_mapping=[("S33", "s33"), ("Date", "date")],
            plan="standard",
        )
        assert n == 1
        # 差分取得で from_yyyymmdd が指定されていること
        call_kwargs = method.call_args.kwargs
        assert "from_yyyymmdd" in call_kwargs
        assert call_kwargs["from_yyyymmdd"] == "20260310"

    def test_exception_returns_zero(self, db_conn):
        method = MagicMock(side_effect=Exception("403 Forbidden"))

        n = _fetch_markets_tier1(
            method,
            db_conn,
            table="markets_margin_interest",
            key_mapping=[("Code", "code"), ("Date", "date")],
            plan="standard",
        )
        assert n == 0

    def test_fallback_on_empty(self, db_conn):
        """当日データ空 → パラメータなしでフォールバック。"""
        df = FakeDataFrame([{"Code": "72030", "Date": "2026-03-10", "Val": 42}])
        method = MagicMock(side_effect=[FakeDataFrame(), df])

        n = _fetch_markets_tier1(
            method,
            db_conn,
            table="markets_margin_interest",
            key_mapping=[("Code", "code"), ("Date", "date")],
            plan="standard",
        )
        assert n == 1
        assert method.call_count == 2

    def test_backfill_with_date_range(self, db_conn):
        """バックフィル: from/to 指定で取得。"""
        df = FakeDataFrame(
            [
                {"Code": "72030", "Date": "2026-03-01", "Val": 1},
                {"Code": "72030", "Date": "2026-03-02", "Val": 2},
            ]
        )
        method = MagicMock(return_value=df)

        n = _fetch_markets_tier1(
            method,
            db_conn,
            table="markets_breakdown",
            key_mapping=[("Code", "code"), ("Date", "date")],
            plan="premium",
            from_yyyymmdd="20260301",
            to_yyyymmdd="20260302",
            incremental=False,
        )
        assert n == 2
        call_kwargs = method.call_args.kwargs
        assert call_kwargs["from_yyyymmdd"] == "20260301"
        assert call_kwargs["to_yyyymmdd"] == "20260302"

    def test_nan_sanitized(self, db_conn):
        """NaN が None に変換されて保存されること。"""
        df = FakeDataFrame(
            [{"Code": "72030", "Date": "2026-03-10", "val": float("nan"), "ok": 1.0}]
        )
        method = MagicMock(return_value=df)

        _fetch_markets_tier1(
            method,
            db_conn,
            table="markets_margin_interest",
            key_mapping=[("Code", "code"), ("Date", "date")],
            plan="standard",
        )

        row = db_conn.execute("SELECT data FROM markets_margin_interest").fetchone()
        data = json.loads(row[0])
        assert data["val"] is None
        assert data["ok"] == 1.0


# ============================================================
# fetch_short_ratio（Tier 1 + Tier 2）
# ============================================================


class TestFetchShortRatio:
    """fetch_short_ratio が Tier 1 と Tier 2 の両方に保存すること。"""

    def test_tier1_and_tier2_populated(self, db_conn):
        rows = [
            {"S33": "0050", "Date": "2026-05-17", "Ratio": 0.35},
            {"S33": "0051", "Date": "2026-05-17", "Ratio": 0.40},
        ]
        df = FakeDataFrame(rows)
        cli = MagicMock()
        cli.get_mkt_short_ratio.return_value = df

        count = fetch_short_ratio(cli, db_conn, plan="standard")

        assert count == 2
        # Tier 1 に保存されていること
        tier1 = db_conn.execute("SELECT COUNT(*) FROM markets_short_ratio").fetchone()[0]
        assert tier1 == 2
        # Tier 2 に保存されていること
        row = db_conn.execute(
            "SELECT data FROM response_cache WHERE cache_key = '/markets/short-ratio'"
        ).fetchone()
        assert row is not None
        data = json.loads(row[0])
        assert len(data) == 2

    def test_tier2_error_does_not_abort(self, db_conn):
        """Tier 2 フェッチ失敗でも Tier 1 の結果を返すこと。"""
        df_tier1 = FakeDataFrame([{"S33": "0050", "Date": "2026-05-17", "Ratio": 0.35}])
        cli = MagicMock()
        # Tier 1 call returns data; Tier 2 (second call) raises
        cli.get_mkt_short_ratio.side_effect = [df_tier1, Exception("timeout")]

        count = fetch_short_ratio(cli, db_conn, plan="standard")
        assert count == 1
        tier1 = db_conn.execute("SELECT COUNT(*) FROM markets_short_ratio").fetchone()[0]
        assert tier1 == 1


# ============================================================
# fetch_short_sale_report（Tier 2 キャッシュキー確認）
# ============================================================


class TestFetchShortSaleReport:
    """fetch_short_sale_report が正しいキーで Tier 2 に保存すること。"""

    def test_tier2_key_no_trailing_pipe(self, db_conn):
        rows = [{"Code": "27800", "DiscDate": "2026-05-17", "ShortBalance": 100}]
        df = FakeDataFrame(rows)
        cli = MagicMock()
        cli.get_mkt_short_sale_report.return_value = df

        fetch_short_sale_report(cli, db_conn, plan="standard")

        # キーに末尾 | がついていないこと
        row = db_conn.execute(
            "SELECT cache_key FROM response_cache WHERE cache_key LIKE '/markets/short-sale-report%'"
        ).fetchone()
        assert row is not None
        assert row[0] == "/markets/short-sale-report"
