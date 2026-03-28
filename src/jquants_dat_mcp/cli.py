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
        default="127.0.0.1",
        help="Bind address for HTTP transport (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=8080,
        help="Port for HTTP transport (default: 8080)",
    )
    parser.add_argument(
        "--ssl-certfile",
        default="",
        help="Path to SSL certificate file",
    )
    parser.add_argument(
        "--ssl-keyfile",
        default="",
        help="Path to SSL private key file",
    )
    parser.add_argument(
        "--bearer-token",
        default="",
        help="Bearer token for authentication (used when OAuth is not configured)",
    )

    # GitHub OAuth 2.1 options
    oauth_group = parser.add_argument_group("GitHub OAuth 2.1")
    oauth_group.add_argument(
        "--github-client-id",
        default="",
        metavar="CLIENT_ID",
        help="GitHub OAuth App client ID (enables OAuth 2.1; overrides GITHUB_CLIENT_ID env var)",
    )
    oauth_group.add_argument(
        "--github-client-secret",
        default="",
        metavar="CLIENT_SECRET",
        help="GitHub OAuth App client secret (overrides GITHUB_CLIENT_SECRET env var)",
    )
    oauth_group.add_argument(
        "--oauth-base-url",
        default="",
        metavar="URL",
        help="Public base URL for OAuth endpoints, e.g. https://mcp.example.com (overrides OAUTH_BASE_URL env var)",
    )

    args = parser.parse_args(argv)

    try:
        from .server import run_server

        run_server(
            transport=args.transport,
            host=args.host,
            port=args.port,
            ssl_certfile=args.ssl_certfile,
            ssl_keyfile=args.ssl_keyfile,
            bearer_token=args.bearer_token,
            github_client_id=args.github_client_id,
            github_client_secret=args.github_client_secret,
            oauth_base_url=args.oauth_base_url,
        )
        return 0
    except KeyboardInterrupt:
        print("\nシャットダウンします。")
        return 0
    except Exception as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
