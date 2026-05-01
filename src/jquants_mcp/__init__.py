"""MCP server for retrieving Japanese stock market data via J-Quants API v2."""

try:
    from jquants_mcp._version import __version__, __version_tuple__
except ImportError:
    try:
        from importlib.metadata import version as _metadata_version

        __version__ = _metadata_version("jquants-mcp")
        __version_tuple__ = tuple(int(p) if p.isdigit() else p for p in __version__.split("."))
    except Exception:
        __version__ = "0.0.0+unknown"
        __version_tuple__ = (0, 0, 0, "unknown")
