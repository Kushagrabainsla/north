"""The server starts the MCP servers in ~/.north/mcp.json and offers their tools.

Nothing called the manager before, so a configured server was never loaded.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mcp.manager import McpManager
from orchestrator.app import _serve_mcp
from tools.models import ToolInput
from tools.registry import ToolRegistry

FAKE = str(Path(__file__).parent / "fake_mcp_server.py")


def _config(path: Path, servers: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


async def test_a_configured_server_s_tools_are_offered_and_answer(tmp_path, move_north_home) -> None:
    home = tmp_path / "home"
    move_north_home(home)
    inbox = [{"id": "m1", "body": "3 new jobs for Software Engineer"}]
    _config(
        home / "mcp.json",
        {"gmail": {"command": sys.executable, "args": [FAKE], "env": {"FAKE_MCP_MESSAGES": json.dumps(inbox)}}},
    )
    registry = ToolRegistry(auto_register=False)

    manager = McpManager(workspace_root=None)  # started as north's lifespan starts them
    await _serve_mcp(manager, registry)
    try:
        tool = next(t for t in registry.all_tools() if "search_gmail_messages" in t.name)
        out = await tool.execute(ToolInput(params={"query": "from:jobalerts-noreply@linkedin.com"}))
    finally:
        await manager.close_all()

    assert out.success, out.error
    assert "3 new jobs" in str(out.data)


async def test_a_project_s_own_mcp_json_is_never_started(monkeypatch, tmp_path, move_north_home) -> None:
    """Starting a server runs the command its file names: a cloned repository must not get to choose it."""
    move_north_home(tmp_path / "home")
    monkeypatch.chdir(tmp_path)
    _config(tmp_path / ".north" / "mcp.json", {"evil": {"command": sys.executable, "args": [FAKE]}})
    _config(tmp_path / "mcp.json", {"evil2": {"command": sys.executable, "args": [FAKE]}})
    registry = ToolRegistry(auto_register=False)

    manager = McpManager(workspace_root=None)  # started as north's lifespan starts them
    await _serve_mcp(manager, registry)
    await manager.close_all()

    assert not [t for t in registry.all_tools() if "search_gmail_messages" in t.name]


async def test_a_server_that_cannot_start_is_skipped(tmp_path, move_north_home) -> None:
    home = tmp_path / "home"
    move_north_home(home)
    _config(
        home / "mcp.json",
        {"broken": {"command": "/no/such/binary"}, "gmail": {"command": sys.executable, "args": [FAKE]}},
    )
    registry = ToolRegistry(auto_register=False)

    manager = McpManager(workspace_root=None)  # started as north's lifespan starts them
    await _serve_mcp(manager, registry)
    await manager.close_all()

    assert [t for t in registry.all_tools() if "search_gmail_messages" in t.name]
