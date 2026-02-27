#!/usr/bin/env python3
"""Stdio-to-Streamable HTTP proxy for MCP.

Claude Desktop (stdio) <-> this proxy <-> remote MCP server (Streamable HTTP)

Usage:
    python mcp-stdio-proxy.py [URL]

    URL defaults to http://m1.local:8080/mcp
"""

import json
import sys

import httpx

DEFAULT_URL = "http://m1.local:8080/mcp"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def main():
    remote_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    session_id = None
    client = httpx.Client(timeout=60)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        headers = dict(HEADERS)
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        try:
            resp = client.post(remote_url, content=line, headers=headers)
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
