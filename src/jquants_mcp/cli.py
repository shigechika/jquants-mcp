"""CLI entry point for jquants-mcp."""

from __future__ import annotations

import argparse
import configparser
import sys
from pathlib import Path

from . import __version__


def _config_ini_path() -> Path:
    """Return the XDG config path used by Settings (see config.py)."""
    import os

    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) / "jquants-mcp" if xdg else Path.home() / ".config" / "jquants-mcp"
    return base / "config.ini"


def _write_api_key(api_key: str) -> Path:
    """Persist the API key to ``~/.config/jquants-mcp/config.ini``.

    Creates the file if missing, preserves existing sections/keys. Sets
    mode 0600 on POSIX so the secret is not world-readable.
    """
    import os

    path = _config_ini_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    cfg = configparser.ConfigParser()
    if path.exists():
        cfg.read(path, encoding="utf-8")
    if "jquants" not in cfg:
        cfg["jquants"] = {}
    cfg["jquants"]["api_key"] = api_key

    with open(path, "w", encoding="utf-8") as f:
        cfg.write(f)

    if os.name != "nt":
        os.chmod(path, 0o600)
    return path


def _cmd_login(args: argparse.Namespace) -> int:
    """Run the browser-based PKCE login flow."""
    from .oauth_login import LoginError, perform_login

    try:
        result = perform_login(base_url=args.base_url, open_browser=not args.no_browser)
    except LoginError as exc:
        print(f"login failed: {exc}", file=sys.stderr)
        return 1

    path = _write_api_key(result.api_key)
    print(f"API key saved to {path}")
    print(
        "You can now start the server normally (e.g. `jquants-mcp`) — "
        "the key is picked up automatically."
    )
    return 0


def _cmd_logout(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Clear the locally-saved API key."""
    path = _config_ini_path()
    if not path.exists():
        print(f"No credentials found at {path}")
        return 0

    cfg = configparser.ConfigParser()
    cfg.read(path, encoding="utf-8")
    if "jquants" in cfg and "api_key" in cfg["jquants"]:
        del cfg["jquants"]["api_key"]
        with open(path, "w", encoding="utf-8") as f:
            cfg.write(f)
        print(f"Cleared api_key from {path}")
    else:
        print(f"No api_key entry in {path}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Start the MCP server (default when no subcommand is given)."""
    from .server import run_server

    run_server()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the CLI."""
    parser = argparse.ArgumentParser(
        prog="jquants-mcp",
        description="MCP server for J-Quants API v2 data retrieval",
    )
    parser.add_argument(
        "--version",
        "-v",
        action="version",
        version=f"jquants-mcp {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command")

    login = subparsers.add_parser(
        "login",
        help="Log in to J-Quants via browser (PKCE) and save API key locally",
    )
    login.add_argument(
        "--base-url",
        default="https://api.jquants.com/v2",
        help="J-Quants API base URL (default: %(default)s)",
    )
    login.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the authorize URL instead of opening the browser",
    )

    subparsers.add_parser(
        "logout",
        help="Clear the locally-saved API key",
    )

    # No subcommand means serve: the server takes no options (stdio only), so
    # the bare ``jquants-mcp`` invocation needs no extra top-level arguments.
    args = parser.parse_args(argv)

    try:
        if args.command == "login":
            return _cmd_login(args)
        if args.command == "logout":
            return _cmd_logout(args)
        return _cmd_serve(args)
    except KeyboardInterrupt:
        print("\nShutting down.")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
