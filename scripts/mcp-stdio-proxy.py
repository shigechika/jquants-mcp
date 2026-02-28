#!/usr/bin/env python3
"""Stdio-to-Streamable HTTP proxy for MCP.

Claude Desktop (stdio) <-> this proxy <-> remote MCP server (Streamable HTTP)

Usage:
    python mcp-stdio-proxy.py [URL] [--bearer-token TOKEN]
"""

import argparse
import json
import sys

import httpx

DEFAULT_URL = "http://192.0.2.1:8080/mcp"


def main():
    parser = argparse.ArgumentParser(description="MCP stdio-to-HTTP proxy")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL, help="Remote MCP server URL")
    parser.add_argument("--bearer-token", default="", help="Bearer token for authentication")
    args = parser.parse_args()

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if args.bearer_token:
        headers["Authorization"] = f"Bearer {args.bearer_token}"

    session_id = None
    client = httpx.Client(timeout=60)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        req_headers = dict(headers)
        if session_id:
            req_headers["Mcp-Session-Id"] = session_id

        try:
            resp = client.post(args.url, content=line, headers=req_headers)
        except Exception as e:
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
