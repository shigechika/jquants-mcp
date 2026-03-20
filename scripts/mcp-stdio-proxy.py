#!/usr/bin/env python3
"""Stdio-to-Streamable HTTP proxy for MCP.

Claude Desktop (stdio) <-> this proxy <-> remote MCP server (Streamable HTTP)

Usage:
    python mcp-stdio-proxy.py [URL] [--bearer-token TOKEN]
"""

import argparse
import json
import sys
import time

import httpx

DEFAULT_URL = "http://192.0.2.1:8080/mcp"
MAX_RETRIES = 3
RETRY_DELAY = 1  # seconds


def log(msg: str) -> None:
    """Log to stderr (visible in Claude Desktop logs)."""
    print(f"[mcp-stdio-proxy] {msg}", file=sys.stderr, flush=True)


def send_request(
    client: httpx.Client,
    url: str,
    content: str,
    headers: dict[str, str],
) -> httpx.Response:
    """Send a request with retry logic."""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.post(url, content=content, headers=headers)
            return resp
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as e:
            last_error = e
            log(f"attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
        except Exception as e:
            raise e
    raise last_error


def main():
    parser = argparse.ArgumentParser(description="MCP stdio-to-HTTP proxy")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL, help="Remote MCP server URL")
    parser.add_argument("--bearer-token", default="", help="Bearer token for authentication")
    args = parser.parse_args()

    log(f"connecting to {args.url}")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if args.bearer_token:
        headers["Authorization"] = f"Bearer {args.bearer_token}"

    session_id = None
    # connect timeout は短く、read timeout は長めに
    client = httpx.Client(timeout=httpx.Timeout(connect=10, read=120, write=30, pool=10))

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        req_headers = dict(headers)
        if session_id:
            req_headers["Mcp-Session-Id"] = session_id

        try:
            resp = send_request(client, args.url, line, req_headers)
        except Exception as e:
            log(f"request failed after retries: {e}")
            # セッションが切れた可能性があるのでリセット
            session_id = None
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "error": {"code": -32000, "message": str(e)},
                        "id": None,
                    }
                ),
                flush=True,
            )
            continue

        # セッション切れ（404 = session not found）の場合はリセットしてリトライ
        if resp.status_code == 404 and session_id:
            log("session expired, resetting session_id and retrying")
            session_id = None
            req_headers = dict(headers)
            try:
                resp = send_request(client, args.url, line, req_headers)
            except Exception as e:
                log(f"retry after session reset failed: {e}")
                print(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "error": {"code": -32000, "message": str(e)},
                            "id": None,
                        }
                    ),
                    flush=True,
                )
                continue

        if "mcp-session-id" in resp.headers:
            session_id = resp.headers["mcp-session-id"]

        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            for event_line in resp.text.splitlines():
                if event_line.startswith("data: "):
                    print(event_line[6:], flush=True)
        else:
            if resp.text.strip():
                print(resp.text.strip(), flush=True)


if __name__ == "__main__":
    main()
