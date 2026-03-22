"""Input validation helpers for MCP tool parameters."""

from __future__ import annotations

import re

# Stock codes: 4- or 5-digit numeric strings (J-Quants uses 5-digit, 4-digit is also accepted)
_CODE_RE = re.compile(r"^\d{4,5}$")

# Dates: YYYYMMDD or YYYY-MM-DD
_DATE_RE = re.compile(r"^\d{4}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])$")
_DATE_WITH_HYPHENS_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])$")

# Valid sector-33 codes (TSE 33-industry classification, padded to 4 digits)
_VALID_SECTOR33_CODES = frozenset(
    [
        "0050",
        "1050",
        "2050",
        "3050",
        "3100",
        "3150",
        "3200",
        "3250",
        "3300",
        "3350",
        "3400",
        "3450",
        "3500",
        "3550",
        "3600",
        "3650",
        "3700",
        "3750",
        "3800",
        "4050",
        "5050",
        "5100",
        "5150",
        "5200",
        "5250",
        "6050",
        "6100",
        "7050",
        "7100",
        "7150",
        "7200",
        "8050",
        "9050",
    ]
)

# Valid market section identifiers
_VALID_SECTIONS = frozenset({"TSEPrime", "TSEStandard", "TSEGrowth"})


def validate_code(code: str | None, param: str = "code") -> str | None:
    """Validate a J-Quants stock code parameter.

    Args:
        code: Stock code string (4 or 5 digits), or None to skip validation.
        param: Parameter name for error messages.

    Returns:
        Error message string if invalid, None if valid or not provided.
    """
    if code is None:
        return None
    if not _CODE_RE.match(code):
        return f"'{param}' must be a 4- or 5-digit numeric code (e.g. '27800'). Got: '{code}'"
    return None


def validate_date(date: str | None, param: str = "date") -> str | None:
    """Validate a date parameter in YYYYMMDD or YYYY-MM-DD format.

    Args:
        date: Date string to validate, or None to skip validation.
        param: Parameter name for error messages.

    Returns:
        Error message string if invalid, None if valid or not provided.
    """
    if date is None:
        return None
    stripped = date.replace("-", "")
    if not _DATE_RE.match(stripped):
        return f"'{param}' must be in YYYYMMDD or YYYY-MM-DD format. Got: '{date}'"
    return None


def validate_sector33(code: str | None, param: str = "sector33_code") -> str | None:
    """Validate a TSE 33-industry sector code.

    Args:
        code: Sector code string, or None to skip validation.
        param: Parameter name for error messages.

    Returns:
        Error message string if invalid, None if valid or not provided.
    """
    if code is None:
        return None
    if code not in _VALID_SECTOR33_CODES:
        return (
            f"'{param}' must be a valid TSE 33-industry code (4-digit, e.g. '0050'). Got: '{code}'"
        )
    return None


def validate_section(section: str | None, param: str = "section") -> str | None:
    """Validate a TSE market section identifier.

    Args:
        section: Section string, or None to skip validation.
        param: Parameter name for error messages.

    Returns:
        Error message string if invalid, None if valid or not provided.
    """
    if section is None:
        return None
    if section not in _VALID_SECTIONS:
        return f"'{param}' must be one of: {', '.join(sorted(_VALID_SECTIONS))}. Got: '{section}'"
    return None


def collect_errors(*error_msgs: str | None) -> list[str]:
    """Collect non-None validation error messages into a list.

    Args:
        *error_msgs: Results from validate_* functions.

    Returns:
        List of error message strings (empty if all valid).
    """
    return [msg for msg in error_msgs if msg is not None]


def make_validation_error_response(errors: list[str]) -> dict:
    """Build a standard error response dict for validation failures.

    Args:
        errors: Non-empty list of validation error messages.

    Returns:
        MCP-compatible error response dict.
    """
    return {
        "error": True,
        "error_type": "ValidationError",
        "message": "; ".join(errors),
    }
