"""CLI entry point for j-quants-dat-mcp."""

from __future__ import annotations

import sys

from . import __version__


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the CLI.

    Args:
        argv: Command line arguments (defaults to sys.argv[1:])

    Returns:
        Exit code (0 for success, 1 for error)
    """
    args = argv if argv is not None else sys.argv[1:]

    if "--help" in args or "-h" in args:
        print("j-quants-dat-mcp - MCP server for J-Quants API v2 data retrieval")
        print()
        print("Usage: j-quants-dat-mcp [OPTIONS]")
        print()
        print("Options:")
        print("  --help, -h       Show this help message")
        print("  --version, -v    Show version")
        print()
        print("Configuration (loaded in order, later wins):")
        print("  1. ~/.jquants-api/jquants-api.toml  (API key, auto-detected)")
        print("  2. ~/.config/j-quants-dat-mcp/config.ini")
        print("  3. ./config.ini")
        print("  4. Environment variables")
        print()
        print("Environment variables:")
        print("  JQUANTS_API_KEY      J-Quants API key (auto-detected from toml)")
        print("  JQUANTS_PLAN         Plan: free/light/standard/premium (default: free)")
        print("  JQUANTS_CACHE_DIR    Cache directory path")
        return 0

    if "--version" in args or "-v" in args:
        print(f"j-quants-dat-mcp {__version__}")
        return 0

    try:
        from .server import run_server

        run_server()
        return 0
    except KeyboardInterrupt:
        print("\nシャットダウンします。")
        return 0
    except Exception as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
