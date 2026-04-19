#!/usr/bin/env python3
"""Fetch Cloud Run memory/CPU metrics for a load test window (#72).

Reads a JSONL produced by ``load_test.py`` to discover the test window and
per-phase time ranges, then queries the Cloud Monitoring API v3 for
``container/memory/utilizations`` and ``container/cpu/utilizations`` with
percentile aligners. Prints a per-phase table with absolute MiB / vCPU based
on the current Cloud Run sizing.

Usage:
    uv run scripts/collect_metrics.py --jsonl load_test_results/run_xxx.jsonl

The script uses ``gcloud auth print-access-token`` for credentials, so it
inherits whichever account ``gcloud config get-value account`` reports (must
have ``monitoring.timeSeries.list`` on the target project).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

MONITORING_ENDPOINT = "https://monitoring.googleapis.com/v3/projects/{project}/timeSeries"

METRICS = {
    "mem": "run.googleapis.com/container/memory/utilizations",
    "cpu": "run.googleapis.com/container/cpu/utilizations",
}

# Cloud Run gives "utilizations" as a DISTRIBUTION of per-request ratios.
# ALIGN_PERCENTILE_* collapses each alignment window to one percentile.
ALIGNERS = ["ALIGN_PERCENTILE_50", "ALIGN_PERCENTILE_95", "ALIGN_PERCENTILE_99"]


@dataclass
class Window:
    start: datetime
    end: datetime

    def extend(self, ts: datetime) -> None:
        if ts < self.start:
            self.start = ts
        if ts > self.end:
            self.end = ts


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_jsonl_windows(path: Path) -> tuple[Window, dict[str, Window]]:
    """Return overall test window and per-phase windows from a load-test JSONL."""
    test_start: datetime | None = None
    test_end: datetime | None = None
    phases: dict[str, Window] = {}
    with path.open() as f:
        for line in f:
            if not line.strip():
                continue
            obj = json.loads(line)
            event = obj.get("event")
            if event == "test_start":
                test_start = parse_ts(obj["ts"])
                continue
            if event == "test_end":
                test_end = parse_ts(obj["ts"])
                continue
            phase = obj.get("phase")
            if not phase:
                continue
            # idle markers only have ts_start
            start = parse_ts(obj["ts_start"])
            end = parse_ts(obj.get("ts_end", obj["ts_start"]))
            if phase not in phases:
                phases[phase] = Window(start=start, end=end)
            else:
                phases[phase].extend(start)
                phases[phase].extend(end)
    if test_start is None or test_end is None:
        raise RuntimeError(f"{path}: missing test_start/test_end markers")
    return Window(start=test_start, end=test_end), phases


def gcloud_access_token() -> str:
    r = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        check=True,
        capture_output=True,
        text=True,
    )
    return r.stdout.strip()


def gcloud_project() -> str:
    r = subprocess.run(
        ["gcloud", "config", "get-value", "project"],
        check=True,
        capture_output=True,
        text=True,
    )
    return r.stdout.strip()


def fmt_rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_time_series(
    project: str,
    token: str,
    metric_type: str,
    service: str,
    window: Window,
    aligner: str,
    alignment_period_s: int = 60,
) -> list[dict[str, Any]]:
    """Fetch one aligner for one metric across the whole window."""
    # Pad window by the alignment period so boundary samples land inside.
    pad = timedelta(seconds=alignment_period_s)
    params = {
        "filter": (f'metric.type="{metric_type}" AND resource.labels.service_name="{service}"'),
        "interval.startTime": fmt_rfc3339(window.start - pad),
        "interval.endTime": fmt_rfc3339(window.end + pad),
        "aggregation.alignmentPeriod": f"{alignment_period_s}s",
        "aggregation.perSeriesAligner": aligner,
        "view": "FULL",
    }
    headers = {"Authorization": f"Bearer {token}"}
    series: list[dict[str, Any]] = []
    page_token: str | None = None
    with httpx.Client(timeout=30.0) as client:
        while True:
            q = dict(params)
            if page_token:
                q["pageToken"] = page_token
            url = MONITORING_ENDPOINT.format(project=project)
            r = client.get(url, params=q, headers=headers)
            if r.status_code != 200:
                raise RuntimeError(
                    f"Monitoring API {metric_type} {aligner}: HTTP {r.status_code}: {r.text[:300]}"
                )
            body = r.json()
            series.extend(body.get("timeSeries", []))
            page_token = body.get("nextPageToken")
            if not page_token:
                break
    return series


def extract_points(series_list: list[dict[str, Any]]) -> list[tuple[datetime, float]]:
    """Flatten all points from all time series into (end_time, value) pairs."""
    out: list[tuple[datetime, float]] = []
    for ts in series_list:
        for p in ts.get("points", []):
            end = parse_ts(p["interval"]["endTime"])
            v = p["value"]
            # DISTRIBUTION aligned with PERCENTILE becomes a DOUBLE
            if "doubleValue" in v:
                out.append((end, float(v["doubleValue"])))
            elif "int64Value" in v:
                out.append((end, float(v["int64Value"])))
    return out


def points_in_window(points: list[tuple[datetime, float]], window: Window) -> list[float]:
    return [v for (ts, v) in points if window.start <= ts <= window.end]


def _max(xs: list[float]) -> float:
    return max(xs) if xs else 0.0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--jsonl", type=Path, help="load_test.py output JSONL")
    p.add_argument("--project", default=None, help="GCP project id (default: gcloud config)")
    p.add_argument(
        "--service",
        default=os.environ.get("JQUANTS_CLOUD_RUN_SERVICE", "jquants-mcp"),
        help=(
            "Cloud Run service name (default from JQUANTS_CLOUD_RUN_SERVICE env or 'jquants-mcp')"
        ),
    )
    p.add_argument(
        "--memory-gib",
        type=float,
        default=8.0,
        help="Current Cloud Run memory limit (GiB), used to convert ratios to MiB",
    )
    p.add_argument(
        "--vcpu",
        type=float,
        default=2.0,
        help="Current Cloud Run vCPU limit, used to convert ratios to cores",
    )
    p.add_argument("--start", help="Override test start (RFC3339)")
    p.add_argument("--end", help="Override test end (RFC3339)")
    args = p.parse_args(argv or sys.argv[1:])

    if args.jsonl:
        overall, phases = load_jsonl_windows(args.jsonl)
    elif args.start and args.end:
        overall = Window(start=parse_ts(args.start), end=parse_ts(args.end))
        phases = {}
    else:
        p.error("--jsonl or (--start and --end) required")
        return 2

    if args.start:
        overall.start = parse_ts(args.start)
    if args.end:
        overall.end = parse_ts(args.end)

    project = args.project or gcloud_project()
    token = gcloud_access_token()
    print(
        f"project={project} service={args.service} "
        f"window={fmt_rfc3339(overall.start)}..{fmt_rfc3339(overall.end)}"
    )

    # metric -> aligner -> list of (end_time, ratio)
    data: dict[str, dict[str, list[tuple[datetime, float]]]] = {}
    for short, metric in METRICS.items():
        data[short] = {}
        for aligner in ALIGNERS:
            series = fetch_time_series(project, token, metric, args.service, overall, aligner)
            data[short][aligner] = extract_points(series)
            print(
                f"  fetched {short} {aligner}: "
                f"{sum(len(s.get('points', [])) for s in series)} points"
            )

    mem_total_mib = args.memory_gib * 1024

    def fmt_mem(ratio: float) -> str:
        return f"{ratio * mem_total_mib:7.0f} MiB ({ratio * 100:5.1f}%)"

    def fmt_cpu(ratio: float) -> str:
        return f"{ratio * args.vcpu:5.2f} vCPU ({ratio * 100:5.1f}%)"

    rows = []
    row_windows: list[tuple[str, Window]] = [("OVERALL", overall)]
    row_windows.extend((name, win) for name, win in phases.items())

    for name, win in row_windows:
        mem_p95 = _max(points_in_window(data["mem"]["ALIGN_PERCENTILE_95"], win))
        mem_p99 = _max(points_in_window(data["mem"]["ALIGN_PERCENTILE_99"], win))
        cpu_p95 = _max(points_in_window(data["cpu"]["ALIGN_PERCENTILE_95"], win))
        cpu_p99 = _max(points_in_window(data["cpu"]["ALIGN_PERCENTILE_99"], win))
        rows.append((name, win, mem_p95, mem_p99, cpu_p95, cpu_p99))

    print()
    print(f"{'Phase':<12} {'Duration':>9}  {'mem p95':<24}  {'mem p99':<24}")
    print("-" * 75)
    for name, win, mp95, mp99, _, _ in rows:
        dur = (win.end - win.start).total_seconds()
        print(f"{name:<12} {dur:>7.0f}s  {fmt_mem(mp95):<24}  {fmt_mem(mp99):<24}")

    print()
    print(f"{'Phase':<12} {'Duration':>9}  {'cpu p95':<22}  {'cpu p99':<22}")
    print("-" * 71)
    for name, win, _, _, cp95, cp99 in rows:
        dur = (win.end - win.start).total_seconds()
        print(f"{name:<12} {dur:>7.0f}s  {fmt_cpu(cp95):<22}  {fmt_cpu(cp99):<22}")

    # Sizing verdict
    print()
    overall_mem_p99 = next(r[3] for r in rows if r[0] == "OVERALL")
    peak_mib = overall_mem_p99 * mem_total_mib
    safety_1_5 = peak_mib * 1.5
    safety_2_0 = peak_mib * 2.0
    print(
        f"Peak memory (overall p99): {peak_mib:.0f} MiB "
        f"({overall_mem_p99 * 100:.1f}% of {args.memory_gib:.0f} GiB)"
    )
    print(f"  with 1.5x safety margin: {safety_1_5:.0f} MiB -> need >= {safety_1_5 / 1024:.1f} GiB")
    print(f"  with 2.0x safety margin: {safety_2_0:.0f} MiB -> need >= {safety_2_0 / 1024:.1f} GiB")
    for candidate_gib in (4, 6, 8):
        verdict = "OK" if safety_1_5 <= candidate_gib * 1024 else "TIGHT/FAIL"
        print(f"  {candidate_gib} GiB: {verdict}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
