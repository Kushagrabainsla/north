"""Unit tests for the MCP (Model Context Protocol) subsystem."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

from mcp.client import McpClient
from mcp.manager import McpManager
from mcp.models import McpCallResult, McpConfigFile, McpToolDefinition
from tools.models import ToolInput
from tools.registry import ToolRegistry
from tools.specialized.mcp_tool import McpTool


def test_mcp_config_parsing(tmp_path: Path):
    cfg_data = {
        "mcpServers": {
            "github": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
                "env": {"TOKEN": "secret_123"},
                "disabled": False,
            },
            "sqlite": {
                "command": "uvx",
                "args": ["mcp-server-sqlite"],
                "disabled": True,
            },
        }
    }
    cfg_file = tmp_path / "mcp.json"
    cfg_file.write_text(json.dumps(cfg_data), encoding="utf-8")

    parsed = McpConfigFile.model_validate(cfg_data)
    assert len(parsed.mcp_servers) == 2
    assert parsed.mcp_servers["github"].command == "npx"
    assert parsed.mcp_servers["github"].env == {"TOKEN": "secret_123"}
    assert parsed.mcp_servers["sqlite"].disabled is True


async def test_mcp_tool_execution():
    mock_client = AsyncMock(spec=McpClient)
    mock_client.call_tool.return_value = McpCallResult(
        content=[{"type": "text", "text": "issue created: #42"}],
        isError=False,
    )

    tool_def = McpToolDefinition(
        name="create_issue",
        description="Creates a new GitHub issue",
        inputSchema={"type": "object", "properties": {"title": {"type": "string"}}},
    )

    tool = McpTool(server_name="github", client=mock_client, tool_def=tool_def)
    assert tool.name == "mcp__github__create_issue"
    assert tool.is_mutating is True

    result = await tool.run(ToolInput(params={"title": "Bug in login"}))
    assert result.success is True
    assert result.data["text"] == "issue created: #42"
    mock_client.call_tool.assert_awaited_once_with("create_issue", {"title": "Bug in login"})


async def test_mcp_tool_error_handling():
    mock_client = AsyncMock(spec=McpClient)
    mock_client.call_tool.return_value = McpCallResult(
        content=[{"type": "text", "text": "Invalid token error"}],
        isError=True,
    )

    tool_def = McpToolDefinition(name="delete_repo")
    tool = McpTool(server_name="github", client=mock_client, tool_def=tool_def)

    result = await tool.run(ToolInput(params={"repo": "main"}))
    assert result.success is False
    assert "Invalid token error" in result.error


def test_mcp_manager_discovery_and_registration(tmp_path: Path):
    ws = tmp_path / "my_project"
    ws.mkdir()
    dot_north = ws / ".north"
    dot_north.mkdir()
    (dot_north / "mcp.json").write_text(
        json.dumps({
            "mcpServers": {
                "local_db": {"command": "echo", "args": ["hi"]},
            }
        }),
        encoding="utf-8",
    )

    manager = McpManager(workspace_root=ws)
    configs = manager.load_configs()
    assert "local_db" in configs
    assert configs["local_db"].command == "echo"

    # Test registration with mocked client
    registry = ToolRegistry()
    mock_client = AsyncMock(spec=McpClient)
    mock_client.tools = [
        McpToolDefinition(name="query_db", description="Executes SQL query")
    ]
    manager._clients["local_db"] = mock_client

    registered = manager.register_tools(registry)
    assert registered == 1
    assert "mcp__local_db__query_db" in registry.all_tool_names()
