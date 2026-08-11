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
import os
import ssl
import sys
from contextlib import AsyncExitStack
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


def _first_text(result: Any) -> str | None:
    """Text of the result's first content block, when it carries one."""
    content = getattr(result, "content", None)
    if not content:
        return None
    return getattr(content[0], "text", None)


def _decode(result: Any) -> Any:
    """Normalise a tool result into plain Python data.

    Two distinct shapes reach here. ``--url``/``--stdio`` go through a real
    ``ClientSession`` speaking wire JSON-RPC, which yields a ``CallToolResult``
    (``.structuredContent``/``.content``, camel-cased because those names come
    straight from the protocol schema) regardless of what SDK the server is
    built on. In-process mode calls ``jquants_mcp.server.mcp.call_tool()``
    directly — the official SDK's ``FastMCP.call_tool(..., convert_result=True)``
    returns ``(unstructured_content, structured_content)`` for a
    ``dict[str, Any]``-annotated tool, or a bare ``list[ContentBlock]``
    otherwise.
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
    if getattr(result, "isError", False):
        # ``ClientSession.call_tool`` *returns* a failed call rather than
        # raising one, but raising is what the harness is built around: it
        # turns the exception into a FAIL carrying the server's own message.
        # Decoding the error text as if it were a payload would instead report
        # the useless "text payload with nothing asserted".
        raise RuntimeError(_first_text(result) or "tool reported an error with no message")
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    text = _first_text(result)
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


async def _http_caller(
    url: str, token: str, insecure: bool
) -> tuple[Caller, list[str], AsyncExitStack]:
    import httpx
    from mcp.client.streamable_http import streamable_http_client

    verify: Any = True
    if insecure:
        # Self-signed certificates are the norm for the on-prem deployment.
        verify = ssl.create_default_context()
        verify.check_hostname = False
        verify.verify_mode = ssl.CERT_NONE

    # The transport builds its own httpx client when not handed one, but
    # handing it one is the only seam for both bearer auth and --insecure. The
    # timeouts replicate the SDK's own create_mcp_http_client defaults, since
    # httpx's 5s default would cut the slower tools off mid-probe.
    client = httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"} if token else None,
        timeout=httpx.Timeout(30.0, read=300.0),
        follow_redirects=True,
        verify=verify,
    )
    stack = AsyncExitStack()
    # A client the transport was handed is a client the transport will not
    # close, so its lifetime is ours. Registering the callback rather than
    # entering the client keeps this step unable to raise before the guard in
    # _client_caller takes over.
    stack.push_async_callback(client.aclose)
    return await _client_caller(stack, streamable_http_client(url, http_client=client))


async def _stdio_caller(command: str) -> tuple[Caller, list[str], AsyncExitStack]:
    """Speak MCP over stdio to a server launched as a subprocess.

    Transport-level, so it works against any MCP server regardless of which
    SDK it is built on — the in-process mode above is FastMCP-specific. This
    is the mode a sibling server would use to reuse this harness.
    """
    import shlex

    from mcp.client.stdio import StdioServerParameters, stdio_client

    parts = shlex.split(command)
    if not parts:
        raise ValueError("--stdio needs a command to launch the server")
    # env is spelled out because the SDK's default gives the child a six-name
    # safelist (HOME, PATH, SHELL, ...). A server reading its API key or cache
    # location from the environment would come up misconfigured under it, and
    # every probe would report that as the tools being broken.
    server = StdioServerParameters(command=parts[0], args=parts[1:], env=dict(os.environ))
    return await _client_caller(AsyncExitStack(), stdio_client(server))


# A page budget rather than a bare ``while``: the smoke test must not be
# hangable by the server it is auditing. Far past any real registry — this
# server's tools arrive in a single page.
_MAX_TOOL_PAGES = 200


async def _list_all_tools(session: Any) -> list[str]:
    """Every registered tool name, following ``tools/list`` pagination to the end.

    The fastmcp client this replaced followed ``nextCursor`` itself; the
    official SDK returns one page per call. Reading only the first page would
    quietly shrink the run to the tools that fit on it *and* report the rest as
    probe specs for tools "no longer registered" — a green exit that never
    called half the tools, which is the failure this script exists to catch.
    """
    from mcp.types import PaginatedRequestParams

    names: list[str] = []
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(_MAX_TOOL_PAGES):
        params = PaginatedRequestParams(cursor=cursor) if cursor is not None else None
        page = await session.list_tools(params=params)
        names.extend(tool.name for tool in page.tools)
        cursor = page.nextCursor
        if cursor is None:
            return names
        if cursor in seen:
            raise RuntimeError(
                f"tools/list handed back the cursor {cursor!r} a second time after "
                f"{len(names)} tools: the server is not advancing through its own "
                "pagination, so the tool list cannot be read in full"
            )
        seen.add(cursor)
    raise RuntimeError(
        f"tools/list still had pages left after {_MAX_TOOL_PAGES} of them "
        f"({len(names)} tools); refusing to probe a tool list this harness "
        "cannot finish reading"
    )


async def _client_caller(
    stack: AsyncExitStack, transport: Any
) -> tuple[Caller, list[str], AsyncExitStack]:
    """Enter an MCP session over ``transport`` and expose it as a caller.

    ``stack`` arrives holding whatever the mode had to open before the
    transport and leaves holding the session as well, so the connection stays
    the single handle the caller has to close.
    """
    from mcp import ClientSession

    try:
        # stdio yields (read, write); streamable HTTP appends a session-id
        # getter this harness has no use for.
        read, write, *_ = await stack.enter_async_context(transport)
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        names = await _list_all_tools(session)
    except BaseException:
        # The caller only learns about the session through our return value, so
        # a failure here would strand it — and in stdio mode that means an
        # orphaned server subprocess holding its pipes open.
        await stack.aclose()
        raise

    async def call(name: str, args: dict[str, Any]) -> Any:
        return _decode(await session.call_tool(name, args))

    return call, names, stack


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

    closer: AsyncExitStack | None = None
    if args.url:
        call, names, closer = await _http_caller(args.url, args.token, args.insecure)
        mode = f"http {args.url}"
    elif args.stdio:
        call, names, closer = await _stdio_caller(args.stdio)
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
        if closer is not None:
            await closer.aclose()

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
