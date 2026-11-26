"""A probe MCP server built with the official Python SDK (mcp 2.3.0).

Run it with the SDK's own interpreter, not north's (north's `mcp/` folder hides the SDK's `mcp` package):

    <sdk venv>/bin/python py_server.py            # stdio, the SDK default: serves both protocol eras
    <sdk venv>/bin/python py_server.py --modern   # stdio, 2026-07-28 only: refuses the initialize handshake
    <sdk venv>/bin/python py_server.py --http PORT  # Streamable HTTP on 127.0.0.1:PORT/mcp
"""

from __future__ import annotations

import sys

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.stdio import stdio_server

server = MCPServer("probe")


@server.tool()
def echo(text: str) -> str:
    """Return the text unchanged."""
    return text


@server.tool()
def big(kilobytes: int) -> str:
    """Return a text of the given size."""
    return "x" * (kilobytes * 1024)


@server.tool()
def fail(reason: str) -> str:
    """Always fail with the reason."""
    raise ValueError(reason)


async def modern_only() -> None:
    from mcp.server.runner import _serve_modern_stream

    low = server._lowlevel_server
    async with stdio_server() as (read, write), low.lifespan(low) as state:
        await _serve_modern_stream(low, read, write, lifespan_state=state, raise_exceptions=False)


if __name__ == "__main__":
    if "--modern" in sys.argv:
        anyio.run(modern_only)
    elif "--http" in sys.argv:
        port = int(sys.argv[sys.argv.index("--http") + 1])
        server.settings.port = port
        server.run(transport="streamable-http", port=port)
    else:
        server.run()
