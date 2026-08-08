#!/usr/bin/env python3
"""Exercise every registered tool against real data and report what is broken.

Unit tests verify logic against fixtures; this verifies that the tools users
actually call still answer with real, fresh data. It exists because the
earnings-calendar tools once returned well-formed *empty* results for every
query while the whole suite stayed green.

Usage:
    # In-process: imports the server, uses the local cache and API key.
    uv run python scripts/smoke_test.py

    # Against a deployed server (also exercises transport and auth).
    uv run python scripts/smoke_test.py --url https://host:8080/mcp --token "$TOKEN"

    # Against any MCP server over stdio — the mode a sibling server would use,
    # since it needs no FastMCP-specific hooks (pair it with its own --probes).
    uv run python scripts/smoke_test.py --stdio "uv run jquants-mcp" --probes smoke_probes

    # Iterate on one spec while writing it.
    uv run python scripts/smoke_test.py --only earnings

    # Machine-readable output for CI.
    uv run python scripts/smoke_test.py --output json

Exit code:
    0  every tool answered acceptably (OK / RESTRICTED / SKIP)
    1  at least one tool FAILED, or is registered with no probe spec
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import ssl
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from smoke_harness import (  # noqa: E402 - needs the sys.path line above
    FAILING,
    JST,
    Caller,
    previous_weekday,
    render_json,
    render_markdown,
    resolve_tokens,
    run_probes,
)


def _decode(result: Any) -> Any:
    """Normalise a tool result into plain Python data.

    Two distinct shapes reach here. ``--url``/``--stdio`` go through a real
    fastmcp ``Client`` speaking wire JSON-RPC, which yields a
    ``CallToolResult``-like object (``.data``/``.structured_content``/
    ``.content``) regardless of what SDK the server is built on. In-process
    mode calls ``jquants_mcp.server.mcp.call_tool()`` directly — the
    official SDK's ``FastMCP.call_tool(..., convert_result=True)`` returns
    ``(unstructured_content, structured_content)`` for a ``dict[str, Any]``-
    annotated tool, or a bare ``list[ContentBlock]`` otherwise.
    """
    if isinstance(result, tuple) and len(result) == 2:
        return result[1]
    if isinstance(result, list):
        if result and hasattr(result[0], "text"):
            try:
                return json.loads(result[0].text)
            except json.JSONDecodeError:
                return result[0].text
        return result
    for attr in ("data", "structured_content"):
        value = getattr(result, attr, None)
        if value is not None:
            return value
    content = getattr(result, "content", None)
    if content:
        text = getattr(content[0], "text", None)
        if text is not None:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return result


async def _in_process_caller() -> tuple[Caller, list[str]]:
    from jquants_mcp import server as server_module

    async def call(name: str, args: dict[str, Any]) -> Any:
        return _decode(await server_module.mcp.call_tool(name, args))

    names = [tool.name for tool in await server_module.mcp.list_tools()]
    return call, names


async def _http_caller(url: str, token: str, insecure: bool) -> tuple[Caller, list[str], Any]:
    from fastmcp import Client
    from fastmcp.client.auth import BearerAuth
    from fastmcp.client.transports import StreamableHttpTransport

    kwargs: dict[str, Any] = {"url": url}
    if token:
        kwargs["auth"] = BearerAuth(token)
    if insecure:
        # Self-signed certificates are the norm for the on-prem deployment.
        import httpx

        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        kwargs["httpx_client_factory"] = lambda **kw: httpx.AsyncClient(**{**kw, "verify": context})

    return await _client_caller(Client(StreamableHttpTransport(**kwargs)))


async def _stdio_caller(command: str) -> tuple[Caller, list[str], Any]:
    """Speak MCP over stdio to a server launched as a subprocess.

    Transport-level, so it works against any MCP server regardless of which
    SDK it is built on — the in-process mode above is FastMCP-specific. This
    is the mode a sibling server would use to reuse this harness.
    """
    import shlex

    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    parts = shlex.split(command)
    if not parts:
        raise ValueError("--stdio needs a command to launch the server")
    return await _client_caller(Client(StdioTransport(command=parts[0], args=parts[1:])))


async def _client_caller(client: Any) -> tuple[Caller, list[str], Any]:
    """Enter an MCP client session and expose it as a caller + tool list."""
    await client.__aenter__()
    try:
        names = [tool.name for tool in await client.list_tools()]
    except BaseException:
        # The caller only learns about the client through our return value, so
        # a failure here would strand the session — and in stdio mode that
        # means an orphaned server subprocess holding its pipes open.
        await client.__aexit__(None, None, None)
        raise

    async def call(name: str, args: dict[str, Any]) -> Any:
        return _decode(await client.call_tool(name, args))

    return call, names, client


async def _resolve_reference(probes_module: Any, call: Caller, today: date) -> tuple[date, str]:
    """Reference business day, from the server when the probes module knows how."""
    fallback = previous_weekday(today)
    hook = getattr(probes_module, "reference_date", None)
    if hook is None:
        return fallback, "weekday fallback (probes module has no reference_date hook)"
    try:
        # The hook may use date tokens; resolve them against the fallback.
        async def bootstrap(name: str, args: dict[str, Any]) -> Any:
            return await call(name, resolve_tokens(args, fallback, today))

        resolved = await asyncio.wait_for(hook(bootstrap), timeout=60)
    except Exception as exc:  # noqa: BLE001 - bootstrap must never abort the run
        return fallback, f"weekday fallback ({type(exc).__name__} while asking the server)"
    if not resolved:
        return fallback, "weekday fallback (server returned no business day)"
    return date.fromisoformat(str(resolved)[:10]), "exchange calendar"


async def main_async(args: argparse.Namespace) -> int:
    probes_module = importlib.import_module(args.probes)
    probes = probes_module.PROBES

    client = None
    if args.url:
        call, names, client = await _http_caller(args.url, args.token, args.insecure)
        mode = f"http {args.url}"
    elif args.stdio:
        call, names, client = await _stdio_caller(args.stdio)
        mode = f"stdio {args.stdio.split()[0]}"
    else:
        call, names = await _in_process_caller()
        mode = "in-process"

    try:
        today = datetime.now(JST).date()
        reference, source = await _resolve_reference(probes_module, call, today)
        if args.date:
            reference = date.fromisoformat(args.date)
            source = "--date override"

        selected = [n for n in names if args.only in n] if args.only else names
        if args.only and not selected:
            print(f"no registered tool matches --only {args.only!r}", file=sys.stderr)
            return 1

        # redact_details is deliberately left off here: this server answers with
        # market data, and an error quoting the code or date it was asked about
        # is exactly what an operator needs. The servers that share this engine
        # and serve personal data turn it on.
        results = await run_probes(
            selected,
            probes,
            call,
            reference,
            today,
            concurrency=args.concurrency,
            show_traceback=args.traceback,
        )
    finally:
        if client is not None:
            await client.__aexit__(None, None, None)

    # A spec for a tool that no longer exists is dead weight that hides drift.
    stale = sorted(set(probes) - set(names))
    if stale and not args.only:
        # stderr: stdout carries the report, and --output json must stay valid.
        print(
            f"::warning::probe specs for tools that are no longer registered: {stale}",
            file=sys.stderr,
        )

    if args.output == "json":
        print(render_json(results, reference, mode))
    else:
        print(render_markdown(results, reference, mode))
        print(f"<sub>reference business day resolved via {source}</sub>")

    return 1 if any(r.status in FAILING for r in results) else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="", help="MCP endpoint URL (omit to run in-process)")
    parser.add_argument(
        "--stdio",
        default="",
        help="launch an MCP server over stdio, e.g. --stdio 'uv run my-mcp'",
    )
    parser.add_argument("--token", default="", help="bearer token for --url")
    parser.add_argument(
        "--insecure", action="store_true", help="skip TLS verification (self-signed certs)"
    )
    parser.add_argument("--probes", default="smoke_probes", help="module holding the probe specs")
    parser.add_argument("--only", default="", help="run only tools whose name contains this")
    parser.add_argument("--date", default="", help="override the reference business day")
    parser.add_argument("--output", choices=("md", "json"), default="md")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--traceback", action="store_true", help="print full stacks for failing tools"
    )
    args = parser.parse_args()

    if args.date:
        try:
            date.fromisoformat(args.date)
        except ValueError:
            parser.error("--date must be YYYY-MM-DD")

    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
