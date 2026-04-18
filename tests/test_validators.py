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
