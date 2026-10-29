"""McpClient against a real stdio server process: large results, a server that
dies mid-call, the call after that, and closing a dead server."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from mcp.client import McpClient, McpError
from mcp.models import McpServerConfig

# A minimal MCP server. Like the official SDK, it sends a tool's result twice:
# as ``content`` text and as ``structuredContent``.
_SERVER = r"""
import json, os, sys

def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    req = json.loads(line)
    method, rid = req.get("method"), req.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2024-11-05",
              "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
            {"name": "big", "inputSchema": {"type": "object"}},
            {"name": "die", "inputSchema": {"type": "object"}},
            {"name": "echo", "inputSchema": {"type": "object"}}]}})
    elif method == "tools/call":
        name, args = req["params"]["name"], req["params"]["arguments"]
        if name == "die":
            os._exit(1)
        text = "x" * args["size"] if name == "big" else args["text"]
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text", "text": text}],
            "structuredContent": {"result": text}, "isError": False}})
"""


@pytest.fixture
def server_script(tmp_path: Path) -> Path:
    path = tmp_path / "fake_mcp_server.py"
    path.write_text(_SERVER, encoding="utf-8")
    return path


async def _client(server_script: Path, timeout: float = 30.0) -> McpClient:
    client = McpClient("fake", McpServerConfig(command=sys.executable, args=[str(server_script)], timeout=timeout))
    await client.start()
    return client


async def test_a_large_result_comes_back_and_the_connection_still_works(server_script: Path):
    client = await _client(server_script, timeout=5.0)
    try:
        result = await client.call_tool("big", {"size": 1_000_000})
        assert result.content[0]["text"] == "x" * 1_000_000

        after = await client.call_tool("echo", {"text": "still here"})
        assert after.content[0]["text"] == "still here"
    finally:
        await client.close()


async def test_a_server_that_dies_mid_call_fails_the_call_at_once(server_script: Path):
    client = await _client(server_script, timeout=30.0)
    try:
        started = time.monotonic()
        with pytest.raises(McpError, match="not running"):
            await client.call_tool("die", {})
        assert time.monotonic() - started < 5.0
    finally:
        await client.close()


async def test_the_call_after_a_crash_raises_mcp_error(server_script: Path):
    client = await _client(server_script, timeout=30.0)
    try:
        with pytest.raises(McpError):
            await client.call_tool("die", {})
        with pytest.raises(McpError, match="not running"):
            await client.call_tool("echo", {"text": "hi"})
    finally:
        await client.close()


async def test_closing_a_server_that_already_exited_does_not_raise(server_script: Path):
    client = await _client(server_script, timeout=30.0)
    with pytest.raises(McpError):
        await client.call_tool("die", {})
    assert client._proc is not None
    await client._proc.wait()

    await client.close()

    assert client._proc is None
