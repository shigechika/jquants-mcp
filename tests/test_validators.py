"""Tests for input validation helpers."""

from jquants_mcp.validators import (
    collect_errors,
    make_validation_error_response,
    validate_code,
    validate_date,
    validate_section,
    validate_sector33,
)


# ---------------------------------------------------------------------------
# validate_code
# ---------------------------------------------------------------------------


def test_validate_code_valid_5digit():
    assert validate_code("27800") is None


def test_validate_code_valid_4digit():
    assert validate_code("2780") is None


def test_validate_code_none():
    assert validate_code(None) is None


def test_validate_code_too_short():
    assert validate_code("123") is not None


def test_validate_code_too_long():
    assert validate_code("123456") is not None


def test_validate_code_non_numeric():
    assert validate_code("AAPL") is not None


def test_validate_code_custom_param_name():
    msg = validate_code("abc", param="instrument_code")
    assert msg is not None
    assert "instrument_code" in msg


# DDDUD = 数字3 + 英大字 + 数字 — J-Quants の現行英数 5桁 code 形式。
# jpx-tickers.csv で 340/4438 件 (7.6%) を占める。Issue #150 参照。
def test_validate_code_alphanumeric_dddud():
    assert validate_code("130A0") is None
    assert validate_code("554A0") is None
    assert validate_code("999Z9") is None


def test_validate_code_rejects_pure_letters():
    assert validate_code("ABCDE") is not None


def test_validate_code_rejects_lowercase_letter():
    # 仕様上 J-Quants は大文字のみ。lowercase は rejected。
    assert validate_code("130a0") is not None


def test_validate_code_rejects_letter_in_wrong_position():
    # DDDUD 以外の英字配置 (e.g. ADDDU, DDUDD 等) は rejected。
    # 将来 J-Quants が新パターンを採用したら _CODE_RE を拡張する。
    assert validate_code("A1234") is not None
    assert validate_code("12A34") is not None
    assert validate_code("1234A") is not None
    assert validate_code("13AA0") is not None  # 英字 2 文字


def test_validate_code_alphanumeric_4char_dddu():
    # Issue #153 — JPX 公式 display 形式 (Kabutan / Yahoo! ファイナンス
    # 等で表示される 4 桁英数 ticker、e.g. 130A) も受理。
    # _normalize_code が 4桁→5桁化 (130A → 130A0) するので cache lookup
    # も自然に動く。これにより PR #151 で書いた
    # `test_validate_code_rejects_4digit_with_letter` の前提は逆転した。
    assert validate_code("130A") is None
    assert validate_code("554A") is None
    assert validate_code("999Z") is None


def test_validate_code_rejects_4char_pure_letters():
    # 4 文字 pure letters は J-Quants 仕様外、引き続き reject。
    assert validate_code("ABCD") is not None


def test_validate_code_rejects_4char_two_letters():
    # 英字 2 文字 (e.g. "12AB") は DDDU パターンに合致しない、reject。
    assert validate_code("12AB") is not None
    assert validate_code("1AB2") is not None


def test_validate_code_rejects_3char_alphanumeric():
    # 3 文字以下は短すぎ、引き続き reject。
    assert validate_code("13A") is not None


# ---------------------------------------------------------------------------
# validate_date
# ---------------------------------------------------------------------------


def test_validate_date_yyyymmdd():
    assert validate_date("20240101") is None


def test_validate_date_with_hyphens():
    assert validate_date("2024-01-01") is None


def test_validate_date_none():
    assert validate_date(None) is None


def test_validate_date_invalid_month():
    assert validate_date("20241301") is not None


def test_validate_date_invalid_day():
    assert validate_date("20240100") is not None


def test_validate_date_bad_format():
    assert validate_date("2024/01/01") is not None


def test_validate_date_custom_param_name():
    msg = validate_date("bad", param="from_date")
    assert msg is not None
    assert "from_date" in msg


# ---------------------------------------------------------------------------
# validate_sector33
# ---------------------------------------------------------------------------


def test_validate_sector33_valid():
    assert validate_sector33("0050") is None


def test_validate_sector33_none():
    assert validate_sector33(None) is None


def test_validate_sector33_invalid():
    assert validate_sector33("9999") is not None


# ---------------------------------------------------------------------------
# validate_section
# ---------------------------------------------------------------------------


def test_validate_section_prime():
    assert validate_section("TSEPrime") is None


def test_validate_section_standard():
    assert validate_section("TSEStandard") is None


def test_validate_section_growth():
    assert validate_section("TSEGrowth") is None


def test_validate_section_none():
    assert validate_section(None) is None


def test_validate_section_invalid():
    assert validate_section("TOPIX") is not None


# ---------------------------------------------------------------------------
# collect_errors / make_validation_error_response
# ---------------------------------------------------------------------------


def test_collect_errors_all_none():
    assert collect_errors(None, None, None) == []


def test_collect_errors_mixed():
    errors = collect_errors(None, "bad code", None, "bad date")
    assert errors == ["bad code", "bad date"]


def test_make_validation_error_response():
    resp = make_validation_error_response(["msg1", "msg2"])
    assert resp["error"] is True
    assert resp["error_type"] == "ValidationError"
    assert "msg1" in resp["message"]
    assert "msg2" in resp["message"]
