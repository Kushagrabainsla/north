"""One approval per site per task in a signed-in profile; anything that could carry data out still asks.

"Open my LinkedIn" asked four times: which profile, use it, check the connection,
open the page. Opening a site asks once and covers reading that site for the rest
of the task. Another site, a click, typing, an upload or a script asks every time,
so what was read on one site can only leave through a call the user saw.

The browser used to keep that memory itself, and with a North-managed profile it
failed: every page asked again, an identical "Open 127.0.0.1 in Research" card
included (issue #76, W7). Now the browser states what a yes covers (a read scope)
and the approval layer remembers it, so these go through `Tool.execute` as a real
call does (CODING_STYLE §7.5).
"""

from __future__ import annotations

import asyncio
import socket
from unittest.mock import AsyncMock, Mock

import pytest

from approval.models import ApprovalDecision
from approval.store import ApprovalStore
from config.approval_mode import ApprovalMode
from config.browser_profiles import BrowserProfile
from config.strategy import NorthSettings
from tests.conftest import bind_approvals
from tools.models import ToolInput, ToolOutput
from tools.universal.browser import BrowserTool, _CommandResult

pytestmark = pytest.mark.usefixtures("chrome_installed")

TASK = "task_linkedin"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Every site resolves to a public address, so the tests need no network."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )


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


def _tool(settings, store: ApprovalStore) -> BrowserTool:
    native = Mock()
    native.call = AsyncMock(
        return_value=ToolOutput(success=True, data={"profile_verified": True, "connection_id": "c1"})
    )
    native.verify = AsyncMock(side_effect=RuntimeError("no page to look at in a test"))
    return bind_approvals(BrowserTool(north_settings=settings, existing_browser=native), ApprovalMode.ASK, store=store)


async def _asks(
    tool: BrowserTool, store: ApprovalStore, task: str = TASK, profile: str = "university", **params
) -> bool:
    """Make the call as an agent would; True when it raised a card, which is then answered yes."""
    call = tool.execute(ToolInput(params={"profile_id": profile, "task_id": task, **params}))
    running = asyncio.create_task(call)
    while not (running.done() or store.pending()):
        await asyncio.sleep(0.01)
    asked = bool(store.pending())
    if asked:
        store.resolve(store.pending()[0].id, ApprovalDecision.APPROVED, decided_by="you")
    await running
    return asked


async def _open(tool: BrowserTool, store: ApprovalStore) -> bool:
    return await _asks(tool, store, action="goto", url="https://www.linkedin.com/feed/")


@pytest.mark.asyncio
async def test_opening_a_site_asks_once_then_reading_it_does_not(settings) -> None:
    store = ApprovalStore()
    tool = _tool(settings, store)

    assert await _open(tool, store)

    assert not await _asks(tool, store, action="inspect")
    assert not await _asks(tool, store, action="extract")
    assert not await _asks(tool, store, action="query", selector="main button", attributes=["aria-label"])
    assert not await _asks(tool, store, action="goto", url="https://linkedin.com/jobs/")
    assert not await _asks(tool, store, action="read", url="https://www.linkedin.com/in/someone/")


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
    store = ApprovalStore()
    tool = _tool(settings, store)
    await _open(tool, store)

    assert await _asks(tool, store, **params)


@pytest.mark.asyncio
async def test_the_approval_belongs_to_one_task_and_one_unedited_profile(settings) -> None:
    store = ApprovalStore()
    tool = _tool(settings, store)
    await _open(tool, store)

    assert await _asks(tool, store, task="task_other", action="goto", url="https://www.linkedin.com/feed/")

    (profile,) = settings.browser_profiles
    settings.set_browser_profiles([profile.model_copy(update={"purpose": "Something else now"})])
    assert await _asks(tool, store, action="inspect")


@pytest.mark.asyncio
async def test_a_preflight_asks_even_after_a_site_was_approved(settings) -> None:
    """It can open the profile in the background when it is not connected."""
    store = ApprovalStore()
    tool = _tool(settings, store)
    await _open(tool, store)

    assert await _asks(tool, store, action="preflight")


@pytest.mark.asyncio
async def test_the_card_says_what_opening_a_site_covers(settings) -> None:
    tool = _tool(settings, ApprovalStore())

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


@pytest.mark.asyncio
async def test_a_north_managed_profile_is_not_described_as_your_existing_one(tmp_path) -> None:
    """Issue #76, W7: every card spoke of an existing profile's sessions and the TaskWindow bridge."""
    managed = NorthSettings(tmp_path / "settings.json")
    managed.set_browser_profiles(
        [BrowserProfile(id="research", name="Research", purpose="Research", context="isolated")]
    )
    tool = BrowserTool(north_settings=managed)

    card = await tool.describe(
        ToolInput(params={"action": "goto", "url": "https://example.org/", "profile_id": "research"})
    )

    assert "North-managed profile" in card.message
    assert "TaskWindow" not in card.message and "Existing profiles" not in card.message


@pytest.mark.asyncio
async def test_in_a_north_managed_profile_reading_an_opened_site_does_not_ask_again(tmp_path) -> None:
    """Issue #76, W7: in the managed "Research" profile every goto and read on the same site asked again."""
    managed = NorthSettings(tmp_path / "settings.json")
    managed.set_browser_profiles(
        [BrowserProfile(id="research", name="Research", purpose="Research", context="isolated")]
    )
    store = ApprovalStore()
    tool = bind_approvals(BrowserTool(north_settings=managed), ApprovalMode.ASK, store=store)
    tool._binary_cmd = ["chrome-agent"]
    tool._chrome_agent = AsyncMock(return_value=_CommandResult(0, '{"ok": true}', ""))

    assert await _asks(tool, store, profile="research", action="goto", url="https://www.example.org/deals.html")

    assert not await _asks(tool, store, profile="research", action="goto", url="https://example.org/verify?s=1")
    assert not await _asks(tool, store, profile="research", action="text")
    assert await _asks(tool, store, profile="research", action="goto", url="https://another.example/")


@pytest.mark.asyncio
async def test_in_autonomous_mode_the_deciders_yes_to_a_site_covers_reading_it(settings) -> None:
    """Issue #83: the memory decider was asked again for every page of a site it had let north open.

    It took 3-9 s a call, most of a browser task's time. Its yes now covers what a person's yes
    covers, and it is told so; another site and any change still go to it.
    """
    from approval.models import DecidedBy
    from approval.policy import Answer

    class _Decider:
        def __init__(self) -> None:
            self.actions = []

        async def rule(self, action, session=None):
            self.actions.append(action)
            return Answer(ApprovalDecision.APPROVED, "", "the user asked for this", DecidedBy.MEMORY_DECIDER)

    decider = _Decider()
    store = ApprovalStore()
    tool = _tool(settings, store)
    bind_approvals(tool, ApprovalMode.AUTONOMOUS, store=store, decider=decider)

    # Opening the page may be on your safe list; the first read on the site then goes to the decider.
    await _asks(tool, store, action="goto", url="https://www.linkedin.com/feed/")
    await _asks(tool, store, action="inspect")
    await _asks(tool, store, action="extract")
    await _asks(tool, store, action="read", url="https://www.linkedin.com/in/someone/")
    assert [a.summary.split()[0] for a in decider.actions] == ["inspect"]
    assert "linkedin.com" in decider.actions[0].scope

    await _asks(tool, store, action="click", uid="n1")
    await _asks(tool, store, action="click", uid="n1")
    assert [a.summary.split()[0] for a in decider.actions] == ["inspect", "click", "click"]


@pytest.mark.asyncio
async def test_a_call_naming_no_saved_profile_is_refused_before_any_card(settings) -> None:
    """Issue #83: a stray bracket in profile_id cost a 28 s decider call before the call was refused."""
    store = ApprovalStore()
    tool = _tool(settings, store)
    output = await tool.execute(ToolInput(params={"profile_id": "university]", "task_id": TASK, "action": "inspect"}))
    assert not output.success and "missing or disabled" in output.error
    assert not store.pending() and not store.all(10)


@pytest.mark.asyncio
async def test_a_card_names_the_rule_its_answer_could_become(settings) -> None:
    """A standing rule names the kind of action and the site of the page, in this profile (approval.standing_rules)."""
    store = ApprovalStore()
    tool = _tool(settings, store)
    await _open(tool, store)

    running = asyncio.create_task(
        tool.execute(ToolInput(params={"profile_id": "university", "task_id": TASK, "action": "fill", "uid": "n1"}))
    )
    while not store.pending():
        await asyncio.sleep(0.01)
    [card] = store.pending()
    store.resolve(card.id, ApprovalDecision.REJECTED, decided_by="you")
    await running

    assert card.rule_scope.startswith("browser fill on linkedin.com in profile university (")


@pytest.fixture
def two_profiles(tmp_path):
    settings = NorthSettings(tmp_path / "two.json")
    settings.set_browser_profiles(
        [
            BrowserProfile(id=pid, name=pid.title(), purpose=pid, context="existing", data_directory=str(tmp_path))
            for pid in ("university", "personal")
        ]
    )
    return settings


@pytest.mark.asyncio
async def test_a_task_keeps_the_profile_it_used_when_a_call_names_none_or_a_wrong_one(two_profiles) -> None:
    """17 of 19 calls refused for their profile came after the task had used one (2026-11-10 to 11-22)."""
    tool = _tool(two_profiles, ApprovalStore())
    await tool.run(ToolInput(params={"profile_id": "personal", "task_id": TASK, "action": "inspect"}))

    for named in ({}, {"profile_id": "84b241ifdce825114"}, {"profile_id": "sjsu"}):
        call = ToolInput(params={"task_id": TASK, "action": "click", "uid": "n1", **named})
        assert (await tool._with_picked_profile(call)).params["profile_id"] == "personal"
    # A real profile the call names is its own choice.
    call = ToolInput(params={"task_id": TASK, "action": "inspect", "profile_id": "university"})
    assert (await tool._with_picked_profile(call)).params["profile_id"] == "university"


@pytest.mark.asyncio
async def test_a_wrong_profile_is_still_refused_when_the_task_has_none_to_keep(two_profiles) -> None:
    store = ApprovalStore()
    tool = _tool(two_profiles, store)

    output = await tool.execute(ToolInput(params={"profile_id": "sjsu", "task_id": "fresh", "action": "inspect"}))

    assert not output.success and "missing or disabled" in output.error
    assert not store.all(10)


@pytest.mark.asyncio
async def test_a_task_that_used_two_profiles_keeps_neither(two_profiles) -> None:
    tool = _tool(two_profiles, ApprovalStore())
    for pid in ("personal", "university"):
        await tool.run(ToolInput(params={"profile_id": pid, "task_id": TASK, "action": "inspect"}))

    call = ToolInput(params={"task_id": TASK, "action": "click", "uid": "n1"})
    assert "profile_id" not in (await tool._with_picked_profile(call)).params


@pytest.mark.asyncio
async def test_the_kept_profile_ends_with_its_task(two_profiles) -> None:
    tool = _tool(two_profiles, ApprovalStore())
    tool._approvals = Mock(return_value=Mock(interaction=None))
    await tool.run(ToolInput(params={"profile_id": "personal", "task_id": TASK, "action": "inspect"}))
    await tool.end_task(TASK)

    call = ToolInput(params={"task_id": TASK, "action": "click", "uid": "n1"})
    assert "profile_id" not in (await tool._with_picked_profile(call)).params
