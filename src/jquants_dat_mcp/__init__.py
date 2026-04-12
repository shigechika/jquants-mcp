"""MCP server for retrieving Japanese stock market data via J-Quants API v2."""

try:
    from jquants_dat_mcp._version import __version__, __version_tuple__
except ImportError:  # editable install without build
    __version__ = "0.0.0+unknown"
    __version_tuple__ = (0, 0, 0, "unknown")
