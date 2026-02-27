"""Tests for scripts/daily_fetch.py.

daily_fetch.py は jpx-short-report の .venv（pandas + jquantsapi 入り）で
動くスクリプトだが、テストは jquants-dat-mcp の .venv で実行する。
pandas を使わず、DataFrame 互換の軽量 mock で API レスポンスを再現する。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import sys

# daily_fetch.py は jquantsapi に依存するが、jquants-dat-mcp の .venv には入っていない。
# テスト用に mock モジュールを差し込む。
_jquantsapi_mock = MagicMock()
sys.modules.setdefault("jquantsapi", _jquantsapi_mock)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from daily_fetch import (  # noqa: E402
    ENDPOINT_MIN_PLAN,
    FETCH_REGISTRY,
    TTL_6H,
    TTL_24H,
    _available_endpoints,
    _ensure_tables,
    _fetch_daily_to_cache,
    _sanitize_row,
    _store_response_cache,
    fetch_earnings_calendar,
    fetch_fins_summary,
    fetch_investor_types,
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
        df = FakeDataFrame([
            {"Date": "2026-02-24", "O": 2700, "C": 2750},
            {"Date": "2026-02-25", "O": 2750, "C": 2800},
        ])
        cli = _mock_cli(get_idx_bars_daily_topix=df)

        n = fetch_topix(cli, db_conn)
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

        n = fetch_topix(cli, db_conn)
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
        n = fetch_topix(cli, db_conn)
        assert n == 0
        cli.get_idx_bars_daily_topix.assert_called_once_with(from_yyyymmdd="20260225")

    def test_empty_response(self, db_conn):
        cli = _mock_cli(get_idx_bars_daily_topix=FakeDataFrame())
        n = fetch_topix(cli, db_conn)
        assert n == 0


# ============================================================
# 決算サマリー
# ============================================================


class TestFetchFinsSummary:
    """fetch_fins_summary のテスト。"""

    def test_basic_fetch(self, db_conn):
        df = FakeDataFrame([
            {"Code": "72030", "DiscDate": "2026-02-25", "Revenue": 1000000},
            {"Code": "99830", "DiscDate": "2026-02-25", "Revenue": 2000000},
        ])
        cli = MagicMock()
        cli.get_fin_summary.side_effect = [df] + [FakeDataFrame()] * 6

        n = fetch_fins_summary(cli, db_conn)
        assert n == 2

        count = db_conn.execute("SELECT COUNT(*) FROM fins_summary").fetchone()[0]
        assert count == 2

    def test_api_error_skips(self, db_conn):
        """API エラー時はスキップして続行。"""
        cli = MagicMock()
        cli.get_fin_summary.side_effect = Exception("API Error")

        n = fetch_fins_summary(cli, db_conn)
        assert n == 0


# ============================================================
# 決算発表予定
# ============================================================


class TestFetchEarningsCalendar:
    """fetch_earnings_calendar のテスト。"""

    def test_basic_fetch(self, db_conn):
        df = FakeDataFrame([
            {"Code": "72030", "Date": "2026-03-01"},
            {"Code": "99830", "Date": "2026-03-15"},
        ])
        cli = _mock_cli(get_eq_earnings_cal=df)

        n = fetch_earnings_calendar(cli, db_conn)
        assert n == 2

        row = db_conn.execute(
            "SELECT data, ttl_seconds FROM response_cache WHERE cache_key=?",
            ("/equities/earnings-calendar",),
        ).fetchone()
        assert row is not None
        assert row[1] == TTL_6H

    def test_empty_response(self, db_conn):
        cli = _mock_cli(get_eq_earnings_cal=FakeDataFrame())
        n = fetch_earnings_calendar(cli, db_conn)
        assert n == 0


# ============================================================
# 投資部門別売買動向
# ============================================================


class TestFetchInvestorTypes:
    """fetch_investor_types のテスト。"""

    def test_basic_fetch(self, db_conn):
        df = FakeDataFrame([
            {"PubDate": "2026-02-20", "Section": "TSEPrime", "FrgnBuy": 100},
            {"PubDate": "2026-02-20", "Section": "TSEStandard", "FrgnBuy": 50},
        ])
        cli = _mock_cli(get_eq_investor_types=df)

        n = fetch_investor_types(cli, db_conn)
        assert n == 2

        count = db_conn.execute("SELECT COUNT(*) FROM investor_types").fetchone()[0]
        assert count == 2

    def test_upsert(self, db_conn):
        """同じ pub_date+section で上書きされること。"""
        df1 = FakeDataFrame([
            {"PubDate": "2026-02-20", "Section": "TSEPrime", "FrgnBuy": 100},
        ])
        df2 = FakeDataFrame([
            {"PubDate": "2026-02-20", "Section": "TSEPrime", "FrgnBuy": 999},
        ])

        cli = _mock_cli(get_eq_investor_types=df1)
        fetch_investor_types(cli, db_conn)

        cli = _mock_cli(get_eq_investor_types=df2)
        fetch_investor_types(cli, db_conn)

        count = db_conn.execute("SELECT COUNT(*) FROM investor_types").fetchone()[0]
        assert count == 1

        row = db_conn.execute("SELECT data FROM investor_types").fetchone()
        data = json.loads(row[0])
        assert data["FrgnBuy"] == 999

    def test_empty_response(self, db_conn):
        cli = _mock_cli(get_eq_investor_types=FakeDataFrame())
        n = fetch_investor_types(cli, db_conn)
        assert n == 0


# ============================================================
# _fetch_daily_to_cache（Standard/Premium 共通ロジック）
# ============================================================


class TestFetchDailyToCache:
    """_fetch_daily_to_cache のテスト。"""

    def test_basic_fetch(self, db_conn):
        df = FakeDataFrame([{"S33": "0050", "Ratio": 0.35}])
        method = MagicMock(return_value=df)

        n = _fetch_daily_to_cache(method, db_conn, "/test/endpoint", TTL_24H)
        assert n == 1

        row = db_conn.execute(
            "SELECT data FROM response_cache WHERE cache_key=?",
            ("/test/endpoint",),
        ).fetchone()
        assert row is not None

    def test_fallback_on_empty(self, db_conn):
        """当日データ空 → パラメータなしでフォールバック。"""
        df = FakeDataFrame([{"val": 42}])
        method = MagicMock(side_effect=[FakeDataFrame(), df])

        n = _fetch_daily_to_cache(method, db_conn, "/test/fallback", TTL_24H)
        assert n == 1
        assert method.call_count == 2

    def test_exception_returns_zero(self, db_conn):
        """API 例外時は 0 を返す。"""
        method = MagicMock(side_effect=Exception("403 Forbidden"))

        n = _fetch_daily_to_cache(method, db_conn, "/test/error", TTL_24H)
        assert n == 0

    def test_fallback_exception(self, db_conn):
        """フォールバックも例外時は 0 を返す。"""
        method = MagicMock(side_effect=[FakeDataFrame(), Exception("timeout")])

        n = _fetch_daily_to_cache(method, db_conn, "/test/err2", TTL_24H)
        assert n == 0

    def test_custom_date_param(self, db_conn):
        """date_param のカスタマイズ（short_sale_report 用）。"""
        df = FakeDataFrame([{"Code": "72030"}])
        method = MagicMock(return_value=df)

        _fetch_daily_to_cache(
            method, db_conn, "/test/custom", TTL_24H,
            date_param="calculated_date",
        )
        call_kwargs = method.call_args_list[0].kwargs
        assert "calculated_date" in call_kwargs

    def test_both_empty(self, db_conn):
        """当日もフォールバックも空の場合。"""
        method = MagicMock(return_value=FakeDataFrame())

        n = _fetch_daily_to_cache(method, db_conn, "/test/both-empty", TTL_24H)
        assert n == 0

    def test_nan_sanitized(self, db_conn):
        """NaN が None に変換されて JSON 保存されること。"""
        df = FakeDataFrame([{"val": float("nan"), "ok": 1.0}])
        method = MagicMock(return_value=df)

        _fetch_daily_to_cache(method, db_conn, "/test/nan", TTL_24H)

        row = db_conn.execute(
            "SELECT data FROM response_cache WHERE cache_key=?",
            ("/test/nan",),
        ).fetchone()
        records = json.loads(row[0])
        assert records[0]["val"] is None
        assert records[0]["ok"] == 1.0
