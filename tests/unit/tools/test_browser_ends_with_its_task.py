"""What a task opened in the browser closes when the task ends, not when north stops.

Issue #76, W1: the North-managed Chromium kept a tab for every task open until
north stopped, one of them on a URL with a secret in it.
"""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, patch

import pytest

from config.browser_profiles import BrowserProfile
from config.strategy import NorthSettings
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.registry import ToolRegistry
from tools.universal.browser import BrowserTool, _CommandResult

_OK = _CommandResult(0, '{"ok": true}', "")


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Every site resolves to a public address, so the tests need no network."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )


def _closes(chrome_agent: AsyncMock) -> list[list[str]]:
    """The close commands north ran, without the binary."""
    return [call.args[0][1:] for call in chrome_agent.await_args_list if call.args[0][-1] == "close"]


def _goto(task_id: str, **params) -> ToolInput:
    return ToolInput(params={"action": "goto", "url": "https://example.org/", "task_id": task_id, **params})


async def test_a_task_s_page_in_a_north_managed_profile_closes_when_the_task_ends(tmp_path) -> None:
    settings = NorthSettings(tmp_path / "settings.json")
    settings.set_browser_profiles(
        [BrowserProfile(id="research", name="Research", purpose="Research", context="isolated")]
    )
    browser = settings.browser_profiles[0].browser_name
    tool = BrowserTool(binary_cmd=["chrome-agent"], north_settings=settings)

    with patch("tools.universal.browser._run_chrome_agent", AsyncMock(return_value=_OK)) as chrome_agent:
        for task in ("t1", "t2"):
            assert (await tool.run(_goto(task, profile_id="research"))).success
        await tool.end_task("t1")
        await tool.end_task("t1")  # a cancelled task can end twice

    assert _closes(chrome_agent) == [["--json", "--browser", browser, "--page", "task-t1", "close"]]


async def test_a_task_s_own_browser_closes_when_the_task_ends() -> None:
    tool = BrowserTool(binary_cmd=["chrome-agent"])

    with patch("tools.universal.browser._run_chrome_agent", AsyncMock(return_value=_OK)) as chrome_agent:
        assert (await tool.run(_goto("t3"))).success
        await tool.end_task("t3")
        await tool.aclose()  # nothing of t3's is left to close when north stops

    assert _closes(chrome_agent) == [["--json", "--browser", "t3", "close"]]


class _Opens(Tool):
    description = "Holds something per task."

    def __init__(self, name: str, fails: bool = False) -> None:
        self.name = name
        self.fails = fails
        self.ended: list[str] = []

    async def run(self, input: ToolInput) -> ToolOutput:
        return ToolOutput(success=True)

    async def end_task(self, task_id: str) -> None:
        if self.fails:
            raise RuntimeError("will not close")
        self.ended.append(task_id)


async def test_every_tool_hears_that_a_task_ended_even_when_another_fails() -> None:
    registry = ToolRegistry(auto_register=False)
    closing = _Opens("b_closing")
    registry.register(_Opens("a_failing", fails=True))
    registry.register(closing)

    await registry.end_task("t1")

    assert closing.ended == ["t1"]
