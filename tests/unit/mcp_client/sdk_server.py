#!/usr/bin/env python3
"""A test MCP server built with the official SDK: `echo` and `big`.

sdk_server.py              stdio, the SDK's default (both protocol eras)
sdk_server.py --modern     stdio, 2026-07-28 only: the initialize handshake is refused
sdk_server.py --http PORT  Streamable HTTP on 127.0.0.1:PORT/mcp; every request needs
                           "Authorization: Bearer test-token", as a hosted server's token would
"""

from __future__ import annotations

import sys

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.stdio import stdio_server

TOKEN = "test-token"
server = MCPServer("sdk-test")


@server.tool()
def echo(text: str) -> str:
    """Return the text unchanged."""
    return text


@server.tool()
def big(kilobytes: int) -> str:
    """Return a text of the given size."""
    return "x" * (kilobytes * 1024)


async def _modern_only() -> None:
    from mcp.server.runner import _serve_modern_stream

    low = server._lowlevel_server
    async with stdio_server() as (read, write), low.lifespan(low) as state:
        await _serve_modern_stream(low, read, write, lifespan_state=state, raise_exceptions=False)


def _http(port: int) -> None:
    import uvicorn
    from starlette.responses import PlainTextResponse

    app = server.streamable_http_app(host="127.0.0.1")

    async def needs_token(scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            if headers.get(b"authorization") != f"Bearer {TOKEN}".encode():
                await PlainTextResponse("no token", status_code=401)(scope, receive, send)
                return
        await app(scope, receive, send)

    uvicorn.run(needs_token, host="127.0.0.1", port=port, log_level="warning", lifespan="on")


if __name__ == "__main__":
    if "--modern" in sys.argv:
        anyio.run(_modern_only)
    elif "--http" in sys.argv:
        _http(int(sys.argv[sys.argv.index("--http") + 1]))
    else:
        server.run()
