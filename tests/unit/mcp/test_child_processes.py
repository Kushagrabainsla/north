"""What north starts gets only what it needs, and does not outlive north.

In the issue #76 test MCP servers inherited north's whole environment,
NORTH_ANTHROPIC_API_KEY included (finding W9); a server that never answered held
startup for its full timeout, one server after another, and was left running
(W8); and a Chromium north started for a browser profile outlived three `north
stop`s with an unauthenticated DevTools port and a tab for every task (W1).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import psutil

from mcp.manager import McpManager
from orchestrator.app import _start_mcp_servers
from tools.models import ToolInput
from tools.registry import ToolRegistry
from tools.universal.browser import BrowserTool

FAKE = str(Path(__file__).parent / "fake_mcp_server.py")
HANG = [sys.executable, "-c", "import time; time.sleep(60)"]


def _config(home: Path, servers: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


async def test_an_mcp_server_gets_a_narrow_environment_and_its_own_block(tmp_path, move_north_home, monkeypatch):
    home = move_north_home(tmp_path / "home")
    dump = tmp_path / "env.json"
    monkeypatch.setenv("NORTH_ANTHROPIC_API_KEY", "sk-ant-not-for-servers")
    monkeypatch.setenv("NORTH_GEMINI_API_KEY", "not-for-servers-either")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
    server = {"command": sys.executable, "args": [FAKE], "env": {"FAKE_MCP_ENV_DUMP": str(dump), "MY_TOKEN": "t"}}
    _config(home, {"files": server})

    manager = await _start_mcp_servers(ToolRegistry(auto_register=False))
    try:
        names = set(json.loads(dump.read_text()))
    finally:
        await manager.close_all()

    assert not {name for name in names if name.startswith("NORTH_")}
    assert {"PATH", "HTTPS_PROXY", "MY_TOKEN"} <= names


async def test_servers_start_together_and_one_that_never_answers_is_stopped(tmp_path, move_north_home):
    home = move_north_home(tmp_path / "home")
    _config(
        home,
        {
            "slow1": {"command": HANG[0], "args": HANG[1:], "timeout": 2},
            "slow2": {"command": HANG[0], "args": HANG[1:], "timeout": 2},
            "files": {"command": sys.executable, "args": [FAKE]},
        },
    )
    manager = McpManager(workspace_root=None)
    before = set(psutil.Process().children(recursive=True))

    started = time.monotonic()
    await manager.start_all()
    took = time.monotonic() - started
    try:
        left = [p for p in psutil.Process().children(recursive=True) if p not in before and p.is_running()]
        assert took < 3.5, f"the two hung servers were waited for one after the other ({took:.1f}s)"
        assert list(manager._clients) == ["files"]
        # Only the server that answered is still running.
        assert len([p for p in left if p.status() != psutil.STATUS_ZOMBIE]) == 1
    finally:
        await manager.close_all()


def _recording_agent(tmp_path: Path) -> tuple[list[str], Path]:
    """A stand-in chrome-agent that records each command line it is given."""
    log = tmp_path / "calls.jsonl"
    script = tmp_path / "chrome_agent.py"
    script.write_text(
        "import json, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print(json.dumps({'ok': True}))\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)], log


async def test_the_browsers_north_started_are_closed_when_north_stops(tmp_path):
    command, log = _recording_agent(tmp_path)
    tool = BrowserTool(binary_cmd=command)
    await tool.run(ToolInput(params={"action": "goto", "url": "https://93.184.215.14/", "task_id": "t1"}))

    await tool.aclose()

    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls[-1] == ["--json", "--browser", "t1", "close"]


async def test_a_browser_already_closed_is_not_closed_again(tmp_path):
    command, log = _recording_agent(tmp_path)
    tool = BrowserTool(binary_cmd=command)
    await tool.run(ToolInput(params={"action": "goto", "url": "https://93.184.215.14/", "task_id": "t1"}))
    await tool.run(ToolInput(params={"action": "close", "task_id": "t1"}))

    await tool.aclose()

    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert sum(call[-1] == "close" for call in calls) == 1


async def test_north_stopping_while_a_server_starts_stops_that_server(tmp_path, move_north_home):
    """Servers start after north is serving, so north can stop while one is still starting."""
    import asyncio

    home = move_north_home(tmp_path / "home")
    _config(home, {"slow": {"command": HANG[0], "args": HANG[1:], "timeout": 30}})
    manager = McpManager(workspace_root=None)
    before = set(psutil.Process().children(recursive=True))
    starting = asyncio.create_task(manager.start_all())
    await asyncio.sleep(0.5)

    starting.cancel()
    await asyncio.gather(starting, return_exceptions=True)

    left = [p for p in psutil.Process().children(recursive=True) if p not in before and p.is_running()]
    assert [p for p in left if p.status() != psutil.STATUS_ZOMBIE] == []
