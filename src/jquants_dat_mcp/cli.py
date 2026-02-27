"""CLI entry point for jquants-dat-mcp."""

from __future__ import annotations

import argparse
import sys

from . import __version__


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the CLI.

    Args:
        argv: Command line arguments (defaults to sys.argv[1:])

    Returns:
        Exit code (0 for success, 1 for error)
    """
    parser = argparse.ArgumentParser(
        prog="jquants-dat-mcp",
        description="MCP server for J-Quants API v2 data retrieval",
    )
    parser.add_argument(
        "--version",
        "-v",
        action="version",
        version=f"jquants-dat-mcp {__version__}",
    )
    parser.add_argument(
        "--transport",
        "-t",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport type (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind address for HTTP transport (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=8080,
        help="Port for HTTP transport (default: 8080)",
    )

    args = parser.parse_args(argv)

    try:
        from .server import run_server

        run_server(transport=args.transport, host=args.host, port=args.port)
        return 0
    except KeyboardInterrupt:
        print("\nシャットダウンします。")
        return 0
    except Exception as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
