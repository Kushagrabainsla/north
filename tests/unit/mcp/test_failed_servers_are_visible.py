"""An MCP server that fails to start is named, with why, to you and to the agent.

In the issue #76 test (W2) a "crm" server whose command did not exist, and a
"calendar" server that never answered, appeared only as WARNINGs in north.log.
The Tools page listed the working server's tools as "BUILT-IN", System had no
MCP section, and the agent, asked to "use the crm", ran find_tools three times
and told the user to confirm they had restarted north.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from orchestrator.api_context import ApiServices, bind_services
from orchestrator.app import _start_mcp_servers
from tests.unit.agents.test_react_loop import _load_agent
from tools.registry import ToolRegistry

FAKE = str(Path(__file__).parent / "fake_mcp_server.py")


def _config(home: Path, servers: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


async def _started(tmp_path: Path, move_north_home) -> ToolRegistry:
    home = tmp_path / "home"
    move_north_home(home)
    _config(
        home,
        {
            "crm": {"command": "/opt/does-not-exist/crm-mcp-server", "args": ["--stdio"]},
            "gmail": {"command": sys.executable, "args": [FAKE]},
        },
    )
    registry = ToolRegistry(auto_register=False)
    manager = await _start_mcp_servers(registry)
    await manager.close_all()
    return registry


async def test_a_server_that_failed_to_start_is_named_with_why(tmp_path: Path, move_north_home) -> None:
    registry = await _started(tmp_path, move_north_home)

    assert registry.unavailable() == {
        "mcp:crm": "MCP server 'crm' is configured in mcp.json but failed to start: "
        "its command '/opt/does-not-exist/crm-mcp-server' was not found"
    }


async def test_the_agent_searching_for_its_tools_is_told_it_failed(tmp_path: Path, move_north_home) -> None:
    registry = await _started(tmp_path, move_north_home)
    agent = _load_agent("general", tmp_path)
    agent._deps.tool_registry = registry

    out = json.loads(await agent._find_tools({"query": "crm customer account"}, {}, None))

    assert any("'crm'" in why and "was not found" in why for why in out["not_available"])


async def test_system_and_tools_name_the_servers(tmp_path: Path, move_north_home) -> None:
    from config.strategy import NorthSettings
    from web.api import system_overview
    from web.extensions import list_tools

    registry = await _started(tmp_path, move_north_home)

    services = ApiServices(
        tool_registry=registry, north_settings=NorthSettings(tmp_path / "settings.json"), north_home=tmp_path
    )
    with bind_services(services):
        overview = await system_overview()
        tools = await list_tools()

    assert [server["name"] for server in overview["mcp_servers"]] == ["gmail"]
    assert any("'crm'" in why for why in overview["not_working"])
    assert {tool["source"] for tool in tools if tool["name"].startswith("mcp__gmail__")} == {"MCP: gmail"}
