"""Structural enforcement of the tool error-handling convention.

Every tool handler that routes an exception through ``format_api_error`` must
catch the shared ``TOOL_API_ERRORS`` tuple, not a hand-written inline tuple.
This is a security guard: the tuple includes ``DecryptionError``, and an
un-caught decrypt failure would leak an unredacted error to the user. Keeping
the catch set as a single named constant means a new tool cannot silently omit
``DecryptionError`` by copy-pasting an incomplete tuple — this test fails if it
tries.

Known gap (intentional): a handler that reaches ``format_api_error`` indirectly
via a helper function is not seen by the AST walk. All current handlers call it
directly, so the walk has full coverage today.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from jquants_mcp.exceptions import (
    TOOL_API_ERRORS,
    DecryptionError,
    JQuantsDatMCPError,
)

_TOOLS_DIR = Path(__file__).resolve().parent.parent / "src" / "jquants_mcp" / "tools"
_TOOL_FILES = sorted(p for p in _TOOLS_DIR.glob("*.py") if p.name != "__init__.py")


def _handlers_calling_format_api_error(tree: ast.AST) -> list[ast.ExceptHandler]:
    """Return every ExceptHandler whose body calls ``format_api_error``."""
    found: list[ast.ExceptHandler] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "format_api_error"
            ):
                found.append(node)
                break
    return found


def test_tool_api_errors_includes_decryption_error():
    """The shared catch set must include DecryptionError (the security invariant)."""
    assert DecryptionError in TOOL_API_ERRORS
    # And every member must be a JQuantsDatMCPError subclass (format_api_error's contract).
    assert all(issubclass(exc, JQuantsDatMCPError) for exc in TOOL_API_ERRORS)


@pytest.mark.parametrize("tool_file", _TOOL_FILES, ids=lambda p: p.name)
def test_every_format_api_error_handler_uses_the_shared_tuple(tool_file: Path):
    """No tool handler may catch an inline tuple instead of TOOL_API_ERRORS."""
    tree = ast.parse(tool_file.read_text(encoding="utf-8"))
    offenders = []
    for handler in _handlers_calling_format_api_error(tree):
        # The caught type must be exactly the Name ``TOOL_API_ERRORS``.
        if not (isinstance(handler.type, ast.Name) and handler.type.id == "TOOL_API_ERRORS"):
            offenders.append(handler.lineno)
    assert not offenders, (
        f"{tool_file.name}: format_api_error handler(s) at line(s) {offenders} "
        "must use `except TOOL_API_ERRORS as e:` (see exceptions.py) instead of an "
        "inline exception tuple — an inline tuple can silently omit DecryptionError."
    )


def test_walk_finds_all_known_handlers():
    """Guard the guard: confirm the AST walk actually sees handlers across the tree.

    If this count drops to zero the enforcement test above would vacuously pass,
    so pin a sane lower bound.
    """
    total = sum(
        len(_handlers_calling_format_api_error(ast.parse(f.read_text(encoding="utf-8"))))
        for f in _TOOL_FILES
    )
    assert total >= 30
