"""One approval per site per task in a signed-in profile; anything that could carry data out still asks.

"Open my LinkedIn" asked four times: which profile, use it, check the connection,
open the page. Now opening a site asks once and covers reading that site for the
rest of the task. Another site, a click, typing, an upload or a script asks every
time, so what was read on one site can only leave through a call the user saw.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from config.browser_profiles import BrowserProfile
from config.strategy import NorthSettings
from tools.models import ToolInput, ToolOutput
from tools.universal.browser import BrowserTool

TASK = "task_linkedin"


@pytest.fixture
def settings(tmp_path):
    settings = NorthSettings(tmp_path / "settings.json")
    settings.set_browser_profiles(
        [
            BrowserProfile(
                id="university",
                name="University",
                purpose="University work",
                context="existing",
                data_directory=str(tmp_path / "chrome"),
            )
        ]
    )
    return settings


def _tool(settings, success: bool = True) -> BrowserTool:
    native = Mock()
    native.call = AsyncMock(
        return_value=ToolOutput(
            success=success,
            data={"profile_verified": True, "connection_id": "c1"} if success else {},
            error=None if success else "Connection not permitted",
        )
    )
    return BrowserTool(north_settings=settings, existing_browser=native)


def _asks(tool: BrowserTool, task: str = TASK, **params) -> bool:
    return tool.mutates({"profile_id": "university", "task_id": task, **params})


async def _open(tool: BrowserTool, url: str = "https://www.linkedin.com/feed/") -> ToolOutput:
    """What runs once the approval for opening the site was given."""
    return await tool.run(ToolInput(params={"action": "goto", "url": url, "profile_id": "university", "task_id": TASK}))


@pytest.mark.asyncio
async def test_opening_a_site_asks_once_then_reading_it_does_not(settings) -> None:
    tool = _tool(settings)
    assert _asks(tool, action="goto", url="https://www.linkedin.com/feed/")

    assert (await _open(tool)).success

    assert not _asks(tool, action="inspect")
    assert not _asks(tool, action="extract")
    assert not _asks(tool, action="goto", url="https://linkedin.com/jobs/")
    assert not _asks(tool, action="read", url="https://www.linkedin.com/in/someone/")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"action": "goto", "url": "https://collector.example/?data=read-from-linkedin"},
        {"action": "click", "uid": "n1"},
        {"action": "fill", "uid": "n1", "value": "x"},
        {"action": "type", "text": "x"},
        {"action": "upload", "filename": "resume.pdf"},
        {"action": "eval", "script": "1"},
    ],
)
async def test_another_site_or_any_change_still_asks(settings, params) -> None:
    tool = _tool(settings)
    await _open(tool)

    assert _asks(tool, **params)


@pytest.mark.asyncio
async def test_the_approval_belongs_to_one_task_and_one_unedited_profile(settings) -> None:
    tool = _tool(settings)
    await _open(tool)

    assert _asks(tool, task="task_other", action="inspect")

    (profile,) = settings.browser_profiles
    settings.set_browser_profiles([profile.model_copy(update={"purpose": "Something else now"})])
    assert _asks(tool, action="inspect")


@pytest.mark.asyncio
async def test_closing_the_tab_ends_the_approval(settings) -> None:
    tool = _tool(settings)
    await _open(tool)

    await tool.run(ToolInput(params={"action": "close", "profile_id": "university", "task_id": TASK}))

    assert _asks(tool, action="inspect")


@pytest.mark.asyncio
async def test_a_failed_visit_approves_nothing(settings) -> None:
    tool = _tool(settings, success=False)

    assert not (await _open(tool)).success
    assert _asks(tool, action="inspect")


@pytest.mark.asyncio
async def test_a_preflight_asks_before_anything_was_approved(settings) -> None:
    """It can open the profile in the background when it is not connected."""
    assert _asks(_tool(settings), action="preflight")


@pytest.mark.asyncio
async def test_the_card_says_what_opening_a_site_covers(settings) -> None:
    tool = _tool(settings)

    opening = await tool.describe(
        ToolInput(params={"action": "goto", "url": "https://www.linkedin.com/feed/", "profile_id": "university"})
    )
    clicking = await tool.describe(ToolInput(params={"action": "click", "uid": "n1", "profile_id": "university"}))

    assert opening.message.startswith(
        "Open linkedin.com in University. For the rest of this task North can read pages on linkedin.com"
    )
    assert "asks before other sites and before clicking, typing, uploading or submitting" in opening.message
    assert clicking.message.startswith("Use University to click on this page.")


def test_the_agent_is_told_not_to_preflight_before_opening_a_page() -> None:
    assert "do not call preflight before it" in BrowserTool.description
