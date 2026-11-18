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


def _in_your_chrome(tmp_path):
    """A browser tool with one existing-Chrome profile, whose bridge answers every call."""
    from unittest.mock import Mock

    settings = NorthSettings(tmp_path / "settings.json")
    settings.set_browser_profiles(
        [BrowserProfile(id="work", name="Work", purpose="Work", context="existing", data_directory=str(tmp_path))]
    )
    bridge = Mock()
    bridge.call = AsyncMock(
        return_value=ToolOutput(success=True, data={"profile_verified": True, "connection_id": "c"})
    )
    bridge.finish = AsyncMock(return_value=True)
    return BrowserTool(north_settings=settings, existing_browser=bridge), bridge


async def test_a_task_s_tab_in_your_chrome_is_saved_as_a_screenshot_and_closed_when_it_ends(tmp_path) -> None:
    """Issue #83: tabs in your own Chrome stayed for good and piled up in North's tab group."""
    tool, bridge = _in_your_chrome(tmp_path)
    assert (await tool.run(_goto("t1", profile_id="work"))).success
    assert (await tool.run(_goto("t2", profile_id="work"))).success

    await tool.end_task("t1")
    await tool.end_task("t1")  # a cancelled task can end twice

    [(profile, task, screenshot)] = [call.args for call in bridge.finish.await_args_list]
    assert (profile.id, task) == ("work", "t1")
    assert screenshot.name == "browser-work.png" and screenshot.parent.name == "t1"


async def test_a_tab_stays_open_while_a_card_of_its_task_waits_for_you(tmp_path) -> None:
    """A draft handed over to send yourself (a card that outlives the task) keeps its tab."""
    from approval.models import Card, CardType
    from approval.store import ApprovalStore
    from tests.conftest import bind_approvals

    tool, bridge = _in_your_chrome(tmp_path)
    store = ApprovalStore()
    bind_approvals(tool, store=store)
    assert (await tool.run(_goto("t1", profile_id="work"))).success
    store.add(
        Card.new(
            type=CardType.APPROVAL,
            task_id="t1",
            agent="north",
            title="Send it?",
            message="The draft is open.",
            blocking=False,
        )
    )

    await tool.end_task("t1")

    bridge.finish.assert_not_awaited()


async def test_finishing_a_tab_closes_it_even_when_the_screenshot_fails(tmp_path) -> None:
    from tools.universal.browser_taskwindow import TaskWindowBrowser, _ToolError

    profile = BrowserProfile(id="work", name="Work", purpose="Work", context="existing", data_directory=str(tmp_path))
    bridge = TaskWindowBrowser(tmp_path)
    bridge._session = lambda profile, task_id: "s1"
    bridge._screenshot = AsyncMock(side_effect=_ToolError("no image", uncertain=False))
    bridge._close = AsyncMock()

    assert not await bridge.finish(profile, "t1", tmp_path / "t1" / "browser-work.png")
    bridge._close.assert_awaited_once_with(profile, "t1")
