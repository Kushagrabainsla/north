"""Unit tests for BrowserTool (tools/universal/browser.py)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.models import ToolInput
from tools.registry import ToolRegistry
from tools.universal.browser import (
    BrowserTool,
    _cdp_http_base,
    _find_chrome_agent_binary,
)


def _isolated(**params):
    return {"browser_context": "isolated", "context_confirmed": True, **params}


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
    tool = BrowserTool(binary_cmd=["chrome-agent"])

    # goto / navigate
    args = tool._build_args("goto", {"url": "https://example.com", "stealth": True}, "task1")
    assert args == ["--json", "--browser", "task1", "goto", "https://example.com", "--stealth", "--inspect"]

    args_nav = tool._build_args("navigate", {"url": "https://example.com"}, "task1")
    assert args_nav == ["--json", "--browser", "task1", "goto", "https://example.com", "--stealth", "--inspect"]

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
    tool = BrowserTool(binary_cmd=["chrome-agent"])

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


def test_browser_classifies_actions_and_existing_profile_access_per_call():
    tool = BrowserTool(binary_cmd=["chrome-agent"])

    assert tool.mutates({"action": "inspect"}) is False
    assert tool.mutates({"action": "click"}) is True
    assert tool.mutates({"action": "goto", "copy_cookies": True}) is True
    assert tool.mutates({"action": "goto", "connect": "9222"}) is True


def test_cdp_endpoint_normalization_is_loopback_only():
    assert _cdp_http_base("9222") == "http://127.0.0.1:9222"
    assert _cdp_http_base("ws://localhost:9222/devtools/browser/example") == "http://localhost:9222"
    with pytest.raises(ValueError, match="localhost only"):
        _cdp_http_base("http://example.com:9222")


@pytest.mark.asyncio
async def test_run_success_json():
    tool = BrowserTool(binary_cmd=["chrome-agent"])

    mock_proc = MagicMock()
    mock_proc.pid = 12345
    mock_proc.returncode = 0
    mock_stdout = json.dumps({"ok": True, "count": 1, "items": [{"name": "Item"}]}).encode("utf-8")
    mock_stderr = b""
    mock_proc.communicate = AsyncMock(return_value=(mock_stdout, mock_stderr))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await tool.run(ToolInput(params=_isolated(action="extract", limit=5)))
        assert output.success is True
        assert output.data["count"] == 1
        assert output.data["items"][0]["name"] == "Item"


@pytest.mark.asyncio
async def test_run_error_with_hint():
    tool = BrowserTool(binary_cmd=["chrome-agent"])

    mock_proc = MagicMock()
    mock_proc.pid = 12345
    mock_proc.returncode = 1
    mock_stdout = json.dumps(
        {
            "ok": False,
            "error": "Node n12 not found in accessibility tree.",
            "hint": "run inspect to refresh element UIDs",
        }
    ).encode("utf-8")
    mock_stderr = b""
    mock_proc.communicate = AsyncMock(return_value=(mock_stdout, mock_stderr))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await tool.run(ToolInput(params=_isolated(action="click", uid="n12")))
        assert output.success is False
        assert "Node n12 not found" in output.error
        assert "Hint: run inspect to refresh element UIDs" in output.error


@pytest.mark.asyncio
async def test_run_assert_unmet_exit_code_2():
    tool = BrowserTool(binary_cmd=["chrome-agent"])

    mock_proc = MagicMock()
    mock_proc.pid = 12345
    mock_proc.returncode = 2
    mock_stdout = json.dumps({"ok": False, "held": False, "actual": "Forbidden"}).encode("utf-8")
    mock_stderr = b""
    mock_proc.communicate = AsyncMock(return_value=(mock_stdout, mock_stderr))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await tool.run(
            ToolInput(params=_isolated(action="assert", assert_type="text", value="Welcome"))
        )
        assert output.success is False
        assert output.data["held"] is False
        assert "Assertion unmet" in output.error


@pytest.mark.asyncio
async def test_ssrf_blocking_for_private_ips():
    tool = BrowserTool(binary_cmd=["chrome-agent"])

    # Attempting to access private metadata IP
    output = await tool.run(
        ToolInput(params=_isolated(action="goto", url="http://169.254.169.254/latest/meta-data"))
    )
    assert output.success is False
    assert "Security policy blocked navigation" in output.error


@pytest.mark.asyncio
async def test_browser_requires_explicit_user_context_choice():
    tool = BrowserTool(binary_cmd=["chrome-agent"])

    missing = await tool.run(ToolInput(params={"action": "status"}))
    assert not missing.success
    assert "Ask the user" in missing.error

    isolated_with_profile = await tool.run(
        ToolInput(params=_isolated(action="status", connect="9222"))
    )
    assert not isolated_with_profile.success
    assert "isolated browser" in isolated_with_profile.error

    existing_without_connection = await tool.run(
        ToolInput(params={"action": "status", "browser_context": "existing", "context_confirmed": True})
    )
    assert not existing_without_connection.success
    assert "requires connect or copy_cookies" in existing_without_connection.error


@pytest.mark.asyncio
async def test_existing_browser_status_performs_real_cdp_preflight():
    tool = BrowserTool(binary_cmd=["chrome-agent"])
    verified = MagicMock(success=True, data={"action": "preflight", "verified": True}, error="")
    with patch("tools.universal.browser._probe_cdp_endpoint", return_value=verified) as probe:
        output = await tool.run(
            ToolInput(
                params={
                    "action": "status",
                    "browser_context": "existing",
                    "context_confirmed": True,
                    "connect": "9222",
                }
            )
        )

    assert output.success is True
    assert output.data == {"action": "preflight", "verified": True}
    probe.assert_called_once_with("9222", 30)


@pytest.mark.asyncio
async def test_failed_cdp_preflight_stops_before_browser_command():
    tool = BrowserTool(binary_cmd=["chrome-agent"])
    failed = MagicMock(success=False, data={}, error="not a DevTools endpoint")
    with (
        patch("tools.universal.browser._probe_cdp_endpoint", return_value=failed),
        patch("asyncio.create_subprocess_exec") as subprocess,
    ):
        output = await tool.run(
            ToolInput(
                params={
                    "action": "goto",
                    "url": "https://linkedin.com/jobs",
                    "browser_context": "existing",
                    "context_confirmed": True,
                    "connect": "9222",
                }
            )
        )

    assert output.success is False
    assert output.error == "not a DevTools endpoint"
    subprocess.assert_not_called()


def test_tool_registry_discovers_browser_tool():
    registry = ToolRegistry(auto_register=True)
    tool = registry.get("browser")
    assert tool is not None
    assert tool.name == "browser"
