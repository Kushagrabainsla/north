"""north reaches every kind of MCP server issue #75 names, through the official SDK.

Before the SDK, north's client spoke only the 2024-11-05 handshake over stdio: a server that serves only the
2026-07-28 protocol refused it, and a hosted server (Streamable HTTP, with a token) could not be named at all.
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from mcp_client.client import McpClient, McpError
from mcp_client.manager import McpManager
from mcp_client.models import McpConfigFile, McpServerConfig
from tools.registry import ToolRegistry

SERVER = str(Path(__file__).parent / "sdk_server.py")


async def _echo(config: McpServerConfig) -> tuple[list[str], str, int]:
    client = McpClient("sdk", config)
    await client.start()
    try:
        echoed = await client.call_tool("echo", {"text": "hello"})
        big = await client.call_tool("big", {"kilobytes": 256})
        return sorted(t.name for t in client.tools), echoed.content[0]["text"], len(big.content[0]["text"])
    finally:
        await client.close()


async def test_a_local_sdk_server_works():
    assert await _echo(McpServerConfig(command=sys.executable, args=[SERVER])) == (
        ["big", "echo"],
        "hello",
        256 * 1024,
    )


async def test_a_server_that_serves_only_the_2026_protocol_works():
    """It refuses the initialize handshake, the only thing north's own client could send."""
    config = McpServerConfig(command=sys.executable, args=[SERVER, "--modern"])

    assert await _echo(config) == (["big", "echo"], "hello", 256 * 1024)


@pytest.fixture
def hosted() -> Iterator[str]:
    """A Streamable HTTP server on this machine that wants a token, as a hosted one does."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen([sys.executable, SERVER, "--http", str(port)])
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
                break
            except OSError:
                if process.poll() is not None:
                    raise RuntimeError("the test server exited") from None
                import time

                time.sleep(0.1)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        process.terminate()
        process.wait(timeout=10)


async def test_a_hosted_server_works_with_its_token(hosted: str):
    config = McpServerConfig(url=hosted, headers={"Authorization": "Bearer test-token"})

    assert await _echo(config) == (["big", "echo"], "hello", 256 * 1024)


async def test_a_hosted_server_without_its_token_fails_to_start(hosted: str):
    client = McpClient("sdk", McpServerConfig(url=hosted, timeout=10))

    with pytest.raises(McpError, match="Failed to start"):
        await client.start()
    await client.close()


async def test_the_manager_offers_a_hosted_servers_tools(hosted: str, tmp_path, move_north_home):
    home = move_north_home(tmp_path / "home")
    home.mkdir(parents=True, exist_ok=True)
    server = {"url": hosted, "headers": {"Authorization": "Bearer test-token"}}
    (home / "mcp.json").write_text(json.dumps({"mcpServers": {"remote": server}}))
    registry = ToolRegistry(auto_register=False)
    manager = McpManager(workspace_root=None)
    try:
        await asyncio.wait_for(manager.start_all(registry), 30)
        assert {"mcp__remote__echo", "mcp__remote__big"} <= {tool.name for tool in registry.all_tools()}
    finally:
        await manager.close_all()


def test_a_server_names_a_command_or_a_url_never_both_or_neither():
    McpServerConfig(command="npx")
    McpServerConfig(url="https://mcp.example.com/mcp")
    with pytest.raises(ValidationError, match="either a command"):
        McpServerConfig()
    with pytest.raises(ValidationError, match="either a command"):
        McpServerConfig(command="npx", url="https://mcp.example.com/mcp")


def test_a_hosted_url_must_be_https_unless_it_is_on_this_machine():
    McpServerConfig(url="http://127.0.0.1:8000/mcp")
    McpServerConfig(url="http://localhost:8000/mcp")
    with pytest.raises(ValidationError, match="https"):
        McpServerConfig(url="http://mcp.example.com/mcp")


def test_mcp_json_accepts_a_hosted_server_next_to_a_local_one():
    config = McpConfigFile.model_validate(
        {
            "mcpServers": {
                "gmail": {"command": "uvx", "args": ["workspace-mcp"]},
                "github": {"url": "https://api.githubcopilot.com/mcp/", "headers": {"Authorization": "Bearer x"}},
            }
        }
    )

    assert config.mcp_servers["github"].url == "https://api.githubcopilot.com/mcp/"
    assert config.mcp_servers["gmail"].command == "uvx"
