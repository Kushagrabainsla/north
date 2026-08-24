"""Unit tests for ChromeAgentTool (tools/universal/chrome_agent.py)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.models import ToolInput
from tools.registry import ToolRegistry
from tools.universal.chrome_agent import (
    ChromeAgentTool,
    _find_chrome_agent_binary,
)


def test_find_chrome_agent_binary():
    # When shutil.which finds it
    with patch("shutil.which", side_effect=lambda x: "/usr/local/bin/chrome-agent" if x == "chrome-agent" else None):
        cmd = _find_chrome_agent_binary()
        assert cmd == ["/usr/local/bin/chrome-agent"]

    # When fallback to npx
    with (
        patch("shutil.which", side_effect=lambda x: "/usr/local/bin/npx" if x == "npx" else None),
        patch("pathlib.Path.is_file", return_value=False),
    ):
        cmd = _find_chrome_agent_binary()
        assert cmd == ["npx", "-y", "chrome-agent"]


def test_build_args_all_actions():
    tool = ChromeAgentTool(binary_cmd=["chrome-agent"])

    # goto
    args = tool._build_args("goto", {"url": "https://example.com", "stealth": True}, "task1")
    assert args == ["--json", "--browser", "task1", "goto", "https://example.com", "--stealth", "--inspect"]

    # inspect
    args = tool._build_args("inspect", {"limit": 50, "uid": "n10"}, "task1")
    assert args == ["--json", "--browser", "task1", "inspect", "--limit", "50", "--uid", "n10"]

    # inspect diff
    args = tool._build_args("inspect", {"diff": True}, "task1")
    assert args == ["--json", "--browser", "task1", "diff"]

    # extract
    args = tool._build_args("extract", {"limit": 10, "query": "pricing"}, "task2")
    assert args == ["--json", "--browser", "task2", "extract", "--limit", "10", "--query", "pricing"]

    # read
    args = tool._build_args("read", {"url": "https://example.com/blog"}, "task1")
    assert args == ["--json", "--browser", "task1", "goto", "https://example.com/blog", "--inspect", "read"]

    # click
    args = tool._build_args("click", {"uid": "n12"}, "task1")
    assert args == ["--json", "--browser", "task1", "click", "n12", "--inspect"]

    # fill
    args = tool._build_args("fill", {"uid": "n20", "value": "test@mail.com"}, "task1")
    assert args == ["--json", "--browser", "task1", "fill", "--uid", "n20", "test@mail.com", "--inspect"]

    # eval
    args = tool._build_args("eval", {"js": "document.title"}, "task1")
    assert args == ["--json", "--browser", "task1", "eval", "document.title"]

    # assert
    args = tool._build_args(
        "assert",
        {"assert_type": "text", "assert_condition": "contains", "value": "Welcome"},
        "task1",
    )
    assert args == ["--json", "--browser", "task1", "assert", "text", "--contains", "Welcome"]

    # close
    args = tool._build_args("close", {}, "task1")
    assert args == ["--json", "--browser", "task1", "close", "--purge"]


def test_format_output():
    tool = ChromeAgentTool(binary_cmd=["chrome-agent"])

    # Format extract
    extract_data = {
        "action": "extract",
        "count": 2,
        "items": [
            {"title": "Story 1", "url": "https://example.com/1"},
            {"title": "Story 2", "url": "https://example.com/2"},
        ],
    }
    fmt = tool.format_output(extract_data)
    assert "### Extracted 2 Records:" in fmt
    assert "| Story 1 |" in fmt

    # Format read
    read_data = {
        "action": "read",
        "article": {"title": "Async Rust", "byline": "Alice", "content": "Async overview..."},
    }
    fmt = tool.format_output(read_data)
    assert "## Async Rust by Alice" in fmt
    assert "Async overview..." in fmt

    # Format assert
    assert_data = {"action": "assert", "held": True, "actual": "Welcome"}
    fmt = tool.format_output(assert_data)
    assert "Assertion [PASS]" in fmt


@pytest.mark.asyncio
async def test_run_success_json():
    tool = ChromeAgentTool(binary_cmd=["chrome-agent"])

    mock_proc = MagicMock()
    mock_proc.pid = 12345
    mock_proc.returncode = 0
    mock_stdout = json.dumps({"ok": True, "count": 1, "items": [{"name": "Item"}]}).encode("utf-8")
    mock_stderr = b""
    mock_proc.communicate = AsyncMock(return_value=(mock_stdout, mock_stderr))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await tool.run(ToolInput(params={"action": "extract", "limit": 5}))
        assert output.success is True
        assert output.data["count"] == 1
        assert output.data["items"][0]["name"] == "Item"


@pytest.mark.asyncio
async def test_run_error_with_hint():
    tool = ChromeAgentTool(binary_cmd=["chrome-agent"])

    mock_proc = MagicMock()
    mock_proc.pid = 12345
    mock_proc.returncode = 1
    mock_stdout = json.dumps({
        "ok": False,
        "error": "Node n12 not found in accessibility tree.",
        "hint": "run inspect to refresh element UIDs",
    }).encode("utf-8")
    mock_stderr = b""
    mock_proc.communicate = AsyncMock(return_value=(mock_stdout, mock_stderr))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await tool.run(ToolInput(params={"action": "click", "uid": "n12"}))
        assert output.success is False
        assert "Node n12 not found" in output.error
        assert "Hint: run inspect to refresh element UIDs" in output.error


@pytest.mark.asyncio
async def test_run_assert_unmet_exit_code_2():
    tool = ChromeAgentTool(binary_cmd=["chrome-agent"])

    mock_proc = MagicMock()
    mock_proc.pid = 12345
    mock_proc.returncode = 2
    mock_stdout = json.dumps({"ok": False, "held": False, "actual": "Forbidden"}).encode("utf-8")
    mock_stderr = b""
    mock_proc.communicate = AsyncMock(return_value=(mock_stdout, mock_stderr))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await tool.run(
            ToolInput(params={"action": "assert", "assert_type": "text", "value": "Welcome"})
        )
        assert output.success is False
        assert output.data["held"] is False
        assert "Assertion unmet" in output.error


@pytest.mark.asyncio
async def test_ssrf_blocking_for_private_ips():
    tool = ChromeAgentTool(binary_cmd=["chrome-agent"])

    # Attempting to access private metadata IP
    output = await tool.run(ToolInput(params={"action": "goto", "url": "http://169.254.169.254/latest/meta-data"}))
    assert output.success is False
    assert "Security policy blocked navigation" in output.error


def test_tool_registry_discovers_chrome_agent():
    registry = ToolRegistry(auto_register=True)
    tool = registry.get("chrome_agent")
    assert tool is not None
    assert tool.name == "chrome_agent"
