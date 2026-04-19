#!/usr/bin/env python3
"""Cloud Run load test for jquants-mcp sizing (#72).

Runs a 6-phase workload against the deployed MCP server and records every
request to a JSONL file. Pair with ``collect_metrics.py`` to correlate the
time window with Cloud Run memory/CPU utilization samples.

Usage:
    # Requires an OAuth token file (get one via `mcp-stdio --oauth`).
    # URL can be set via --url or JQUANTS_CLOUD_RUN_URL env.
    uv run scripts/load_test.py \\
        --url https://your-cloud-run-service.run.app/mcp \\
        --token-file ~/.config/mcp-stdio/tokens.json \\
        --output load_test_results/run_$(date +%Y%m%d_%H%M%S).jsonl

Phases (see issue #72):
    1. warmup    cache_status x1
    2. steady    light tools x30, 2s interval
    3. heavy_mem 5-year daily bars for 5 issues (sequential)
    4. parallel  3-way concurrent heavy queries for ~2 min
    5. burst     10-way concurrent mixed queries for 30 s
    6. cooldown  idle observation for 60 s

A 15 s gap separates each phase to make per-phase windows easy to read from
Cloud Run monitoring (which samples on ~60 s granularity).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

# Phase 3 originally specified "15 years x 5 issues", but Light plan retains
# only 5 years, so we substitute "5 years x 15 issues" to get equivalent row
# counts (and JSON serialization pressure). 15 large-cap issues spanning
# sectors to avoid cache-key collisions.
HEAVY_CODES = [
    "7203",  # Toyota
    "6758",  # Sony
    "9984",  # SoftBank Group
    "8306",  # MUFG
    "9432",  # NTT
    "6861",  # Keyence
    "8035",  # Tokyo Electron
    "9983",  # Fast Retailing
    "8058",  # Mitsubishi Corp
    "8316",  # SMFG
    "6098",  # Recruit
    "4063",  # Shin-Etsu Chem
    "6501",  # Hitachi
    "7974",  # Nintendo
    "8766",  # Tokio Marine
]
LIGHT_TOOLS: list[tuple[str, dict[str, Any]]] = [
    ("health_check", {}),
    ("cache_status", {}),
    ("get_equities_master", {"code": "7203"}),
    ("get_markets_calendar", {}),
    ("get_indices_bars_daily_topix", {"date": "2025-01-06"}),
]

PHASE_GAP_SECONDS = 15
DEFAULT_OUTPUT_DIR = Path("load_test_results")


@dataclass
class RequestResult:
    ts_start: str
    ts_end: str
    phase: str
    tool: str
    args: dict[str, Any]
    latency_ms: float
    status: str  # "ok" | "http_error" | "timeout" | "exception"
    http_status: int | None
    response_bytes: int
    count: int | None
    error: str | None


@dataclass
class Writer:
    path: Path
    _fh: Any = field(default=None, init=False)

    def __enter__(self) -> Writer:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", buffering=1)  # line-buffered
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._fh is not None:
            self._fh.close()

    def write(self, obj: dict[str, Any]) -> None:
        self._fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_tool_result(text: str) -> tuple[dict[str, Any] | str | None, int]:
    """Extract tool result JSON from SSE response. Returns (parsed, byte_size)."""
    byte_size = 0
    parsed: dict[str, Any] | str | None = None
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            obj = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        if "result" not in obj:
            continue
        content = obj["result"].get("content") or []
        if content and content[0].get("type") == "text":
            raw = content[0]["text"]
            byte_size = len(raw.encode("utf-8"))
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = raw
    return parsed, byte_size


class MCPSession:
    """Single MCP session over Streamable HTTP.

    One session per asyncio task so Mcp-Session-Id does not interleave.
    """

    def __init__(self, url: str, token: str, timeout: float = 120.0) -> None:
        self.url = url
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=timeout, write=10, pool=10),
            http2=False,
        )
        self._req_id = 0
        self._session_headers: dict[str, str] | None = None

    async def __aenter__(self) -> MCPSession:
        await self._initialize()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.aclose()

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def _initialize(self) -> None:
        init = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "load_test", "version": "1.0"},
            },
        }
        r = await self._client.post(self.url, content=json.dumps(init), headers=self.headers)
        if r.status_code != 200:
            raise RuntimeError(f"initialize failed: HTTP {r.status_code}: {r.text[:200]}")
        session_id = r.headers.get("mcp-session-id")
        if not session_id:
            raise RuntimeError("initialize response missing Mcp-Session-Id header")
        self._session_headers = dict(self.headers)
        self._session_headers["Mcp-Session-Id"] = session_id
        await self._client.post(
            self.url,
            content=json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
            ),
            headers=self._session_headers,
        )

    async def call(self, tool: str, args: dict[str, Any], phase: str) -> RequestResult:
        assert self._session_headers is not None
        msg = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        }
        ts_start = now_iso()
        t0 = time.perf_counter()
        status = "ok"
        http_status: int | None = None
        error: str | None = None
        parsed: dict[str, Any] | str | None = None
        byte_size = 0
        try:
            r = await self._client.post(
                self.url, content=json.dumps(msg), headers=self._session_headers
            )
            http_status = r.status_code
            if r.status_code != 200:
                status = "http_error"
                error = r.text[:200]
            else:
                parsed, byte_size = parse_tool_result(r.text)
        except httpx.ReadTimeout:
            status = "timeout"
            error = "ReadTimeout"
        except Exception as e:  # noqa: BLE001
            status = "exception"
            error = f"{type(e).__name__}: {e}"
        latency_ms = (time.perf_counter() - t0) * 1000
        count = parsed.get("count") if isinstance(parsed, dict) else None
        return RequestResult(
            ts_start=ts_start,
            ts_end=now_iso(),
            phase=phase,
            tool=tool,
            args=args,
            latency_ms=round(latency_ms, 2),
            status=status,
            http_status=http_status,
            response_bytes=byte_size,
            count=count,
            error=error,
        )


def load_token(path: Path) -> str:
    with path.open() as f:
        tokens = json.load(f)
    if not tokens:
        raise RuntimeError(f"{path} has no entries")
    entry = tokens[next(iter(tokens))]
    tok = entry.get("access_token")
    if not tok:
        raise RuntimeError(f"{path}: first entry has no access_token")
    return tok


def log_result(writer: Writer, res: RequestResult) -> None:
    writer.write(
        {
            "ts_start": res.ts_start,
            "ts_end": res.ts_end,
            "phase": res.phase,
            "tool": res.tool,
            "args": res.args,
            "latency_ms": res.latency_ms,
            "status": res.status,
            "http_status": res.http_status,
            "response_bytes": res.response_bytes,
            "count": res.count,
            "error": res.error,
        }
    )


def phase_summary(phase: str, results: list[RequestResult]) -> str:
    if not results:
        return f"[{phase}] no requests"
    ok = [r for r in results if r.status == "ok"]
    lats = sorted(r.latency_ms for r in ok)
    errs = len(results) - len(ok)

    def pct(p: float) -> float:
        if not lats:
            return 0.0
        k = min(len(lats) - 1, int(round(p * (len(lats) - 1))))
        return lats[k]

    return (
        f"[{phase}] n={len(results)} ok={len(ok)} err={errs} "
        f"p50={pct(0.5):.0f}ms p95={pct(0.95):.0f}ms max={max(lats, default=0):.0f}ms"
    )


async def phase_warmup(session: MCPSession, writer: Writer) -> list[RequestResult]:
    results = [await session.call("cache_status", {}, "warmup")]
    log_result(writer, results[0])
    return results


async def phase_steady(session: MCPSession, writer: Writer) -> list[RequestResult]:
    results: list[RequestResult] = []
    for i in range(30):
        tool, args = LIGHT_TOOLS[i % len(LIGHT_TOOLS)]
        res = await session.call(tool, args, "steady")
        log_result(writer, res)
        results.append(res)
        await asyncio.sleep(2.0)
    return results


async def phase_heavy_mem(session: MCPSession, writer: Writer) -> list[RequestResult]:
    results: list[RequestResult] = []
    for code in HEAVY_CODES:
        res = await session.call("get_equities_bars_daily", {"code": code}, "heavy_mem")
        log_result(writer, res)
        results.append(res)
    return results


async def _parallel_worker(
    url: str,
    token: str,
    phase: str,
    stop_at: float,
    writer: Writer,
    results: list[RequestResult],
    lock: asyncio.Lock,
    pick: callable,
) -> None:
    async with MCPSession(url, token) as session:
        while time.monotonic() < stop_at:
            tool, args = pick()
            res = await session.call(tool, args, phase)
            async with lock:
                log_result(writer, res)
                results.append(res)


async def phase_parallel(
    url: str, token: str, writer: Writer, duration_s: float = 120.0
) -> list[RequestResult]:
    results: list[RequestResult] = []
    lock = asyncio.Lock()
    stop_at = time.monotonic() + duration_s
    rng = random.Random(42)

    def pick() -> tuple[str, dict[str, Any]]:
        return ("get_equities_bars_daily", {"code": rng.choice(HEAVY_CODES)})

    await asyncio.gather(
        *(
            _parallel_worker(url, token, "parallel", stop_at, writer, results, lock, pick)
            for _ in range(3)
        )
    )
    return results


async def phase_burst(
    url: str, token: str, writer: Writer, duration_s: float = 30.0
) -> list[RequestResult]:
    results: list[RequestResult] = []
    lock = asyncio.Lock()
    stop_at = time.monotonic() + duration_s
    rng = random.Random(7)

    def pick() -> tuple[str, dict[str, Any]]:
        # 60% heavy, 40% light to mimic a mixed burst
        if rng.random() < 0.6:
            return ("get_equities_bars_daily", {"code": rng.choice(HEAVY_CODES)})
        return LIGHT_TOOLS[rng.randrange(len(LIGHT_TOOLS))]

    await asyncio.gather(
        *(
            _parallel_worker(url, token, "burst", stop_at, writer, results, lock, pick)
            for _ in range(10)
        )
    )
    return results


async def phase_cooldown(writer: Writer, seconds: float = 60.0) -> list[RequestResult]:
    # Pure idle so metrics show recovery. Emit a marker so the JSONL covers the window.
    writer.write(
        {
            "ts_start": now_iso(),
            "phase": "cooldown",
            "event": "idle_begin",
            "seconds": seconds,
        }
    )
    await asyncio.sleep(seconds)
    writer.write({"ts_start": now_iso(), "phase": "cooldown", "event": "idle_end"})
    return []


async def run_all(args: argparse.Namespace) -> int:
    token = load_token(Path(args.token_file).expanduser())
    output = Path(args.output).expanduser()
    print(f"output: {output}")

    with Writer(output) as writer:
        writer.write(
            {
                "event": "test_start",
                "ts": now_iso(),
                "url": args.url,
                "phases": args.phases,
                "clear_response_cache": args.clear_response_cache,
            }
        )
        print(f"[test] start {now_iso()}")

        async with MCPSession(args.url, token) as session:
            if args.clear_response_cache:
                res = await session.call("cache_clear", {"table": "response_cache"}, "setup")
                log_result(writer, res)
                print(
                    f"[setup] cache_clear(response_cache) -> {res.status} ({res.latency_ms:.0f}ms)"
                )

            if "1" in args.phases:
                print(phase_summary("warmup", await phase_warmup(session, writer)))
                await asyncio.sleep(PHASE_GAP_SECONDS)
            if "2" in args.phases:
                print(phase_summary("steady", await phase_steady(session, writer)))
                await asyncio.sleep(PHASE_GAP_SECONDS)
            if "3" in args.phases:
                print(phase_summary("heavy_mem", await phase_heavy_mem(session, writer)))
                await asyncio.sleep(PHASE_GAP_SECONDS)

            if args.clear_response_cache and "4" in args.phases:
                res = await session.call("cache_clear", {"table": "response_cache"}, "setup")
                log_result(writer, res)
                print(
                    f"[setup] cache_clear(response_cache) before parallel -> "
                    f"{res.status} ({res.latency_ms:.0f}ms)"
                )

        if "4" in args.phases:
            print(
                phase_summary(
                    "parallel",
                    await phase_parallel(args.url, token, writer, args.parallel_seconds),
                )
            )
            await asyncio.sleep(PHASE_GAP_SECONDS)
        if "5" in args.phases:
            print(
                phase_summary(
                    "burst",
                    await phase_burst(args.url, token, writer, args.burst_seconds),
                )
            )
            await asyncio.sleep(PHASE_GAP_SECONDS)
        if "6" in args.phases:
            await phase_cooldown(writer, args.cooldown_seconds)
            print("[cooldown] done")

        writer.write({"event": "test_end", "ts": now_iso()})
        print(f"[test] end   {now_iso()}")

    print(f"\nResults written to {output}")
    print(f"Next: uv run scripts/collect_metrics.py --jsonl {output}")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--url",
        default=os.environ.get("JQUANTS_CLOUD_RUN_URL"),
        help=("MCP endpoint URL (default from JQUANTS_CLOUD_RUN_URL env; required)"),
    )
    p.add_argument(
        "--token-file",
        default=os.environ.get("JQUANTS_TOKEN_FILE", "~/.config/mcp-stdio/tokens.json"),
        help=(
            "OAuth token file produced by `mcp-stdio --oauth` (default "
            "from JQUANTS_TOKEN_FILE env or ~/.config/mcp-stdio/tokens.json)"
        ),
    )
    default_output = DEFAULT_OUTPUT_DIR / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    p.add_argument("--output", default=str(default_output))
    p.add_argument(
        "--phases",
        default="1,2,3,4,5,6",
        help="Comma-separated phase numbers to run (default: all)",
    )
    p.add_argument("--parallel-seconds", type=float, default=120.0)
    p.add_argument("--burst-seconds", type=float, default=30.0)
    p.add_argument("--cooldown-seconds", type=float, default=60.0)
    p.add_argument(
        "--clear-response-cache",
        action="store_true",
        help="Call cache_clear(response_cache) before the run and again before "
        "phase 4 (parallel). Forces DB->JSON serialization on every request "
        "to stress memory. Tier1 row-level cache is NOT touched.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        return asyncio.run(run_all(args))
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
