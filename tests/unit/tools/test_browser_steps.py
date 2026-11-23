"""Step lists: several browser actions in one call, one card and one model turn.

In north's own runs (2026-11-10 to 11-22) a browser task made a median of 31 calls, each after a
model turn of about 2.5 s, and each click waited about 4 s for its own decision. A list runs in
order in the task's tab, stops at the first step that fails, and shows the page at the end. It
never sends or spends: it stops before such a step, which then needs its own card.
"""

from __future__ import annotations

import asyncio
import json
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from approval.models import ApprovalDecision
from approval.store import ApprovalStore
from config.approval_mode import ApprovalMode
from config.browser_profiles import BrowserProfile
from config.strategy import NorthSettings
from tests.conftest import bind_approvals
from tools.models import ToolInput, ToolOutput
from tools.universal.browser import BrowserTool

pytestmark = pytest.mark.usefixtures("chrome_installed")

TASK = "task_steps"
SEARCH = [
    {"action": "fill", "selector": "input[name=q]", "value": "north"},
    {"action": "click", "selector": "button[type=submit]"},
    {"action": "wait", "condition": "text", "pattern": "Results"},
]


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )


@pytest.fixture
def settings(tmp_path):
    settings = NorthSettings(tmp_path / "settings.json")
    settings.set_browser_profiles(
        [BrowserProfile(id="work", name="Work", purpose="Work", context="existing", data_directory=str(tmp_path))]
    )
    return settings


def _page_reader(target_text: str = "Search", *, readable: bool = True):
    """What the page shows the step check: the element a selector names, with *target_text* on it."""

    async def read_page(command, timeout):
        if not readable:
            raise RuntimeError("the page cannot be read")
        page = {"url": "https://search.example/", "title": "Search", "focused": None}
        page["target"] = {"role": "button", "text": target_text, "inForm": False}
        return SimpleNamespace(stdout=json.dumps({"result": json.dumps(page)}))

    return lambda profile, params: read_page


def _bridge(fail_on: str = "", target_text: str = "Search", readable: bool = True):
    """A stand-in bridge that records each action it runs; *fail_on* names one that fails."""
    native = Mock()
    native.ran = []

    async def call(profile, params):
        native.ran.append(params["action"])
        if params["action"] == fail_on:
            return ToolOutput(success=False, error=f"{fail_on} went wrong")
        if params["action"] == "inspect":
            return ToolOutput(success=True, data={"snapshot": 'uid=n1 button "More"', "url": "u", "title": "T"})
        return ToolOutput(success=True, data={"action": params["action"]})

    native.call = AsyncMock(side_effect=call)
    native.verify = AsyncMock(side_effect=RuntimeError("no connection check in a test"))
    native.description_runner = Mock(side_effect=_page_reader(target_text, readable=readable))
    return native


def _call(steps, **params) -> ToolInput:
    return ToolInput(params={"action": "steps", "steps": steps, "profile_id": "work", "task_id": TASK, **params})


async def test_a_list_runs_in_order_in_one_call_and_shows_the_page_at_the_end(settings) -> None:
    native = _bridge()
    tool = BrowserTool(north_settings=settings, existing_browser=native)

    output = await tool.run(_call(SEARCH))

    assert output.success
    assert native.ran == ["fill", "click", "wait", "inspect"]  # one page view, at the end
    assert [step["ok"] for step in output.data["steps"]] == [True, True, True]
    text = tool.format_output(output.data)
    assert "1. fill input[name=q]: ok" in text
    assert "3. wait text Results: ok" in text
    assert '### Page now: T - u\n```yaml\nuid=n1 button "More"' in text


async def test_a_list_stops_at_the_first_step_that_fails(settings) -> None:
    native = _bridge(fail_on="click")
    tool = BrowserTool(north_settings=settings, existing_browser=native)

    output = await tool.run(_call(SEARCH))

    assert not output.success
    assert output.error == "Step 2 (click) failed: click went wrong"
    assert native.ran == ["fill", "click", "inspect"]  # the wait never ran; the page is still shown
    assert output.data["not_run"] == [3]
    assert "Steps 3 did not run." in tool.format_output(output.data)


@pytest.mark.parametrize(("words", "why"), [("Send invitation", "send something"), ("Buy now", "spend money")])
async def test_a_list_stops_before_a_step_that_sends_or_spends(settings, words, why) -> None:
    native = _bridge(target_text=words)
    tool = BrowserTool(north_settings=settings, existing_browser=native)

    output = await tool.run(_call(SEARCH))

    assert not output.success
    assert output.error.startswith(f"Step 2 did not run: This step would {why}")
    assert "Make it its own call" in output.error
    assert native.ran == ["fill", "inspect"]  # the click never reached the page


async def test_a_list_stops_before_a_click_north_cannot_see(settings) -> None:
    native = _bridge(readable=False)
    tool = BrowserTool(north_settings=settings, existing_browser=native)

    output = await tool.run(_call(SEARCH))

    assert not output.success and "could not see what this step lands on" in output.error
    assert native.ran == ["fill", "inspect"]


async def test_a_wait_in_a_list_waits_at_most_15_seconds(settings) -> None:
    native = _bridge()
    tool = BrowserTool(north_settings=settings, existing_browser=native)

    await tool.run(_call([{"action": "wait", "condition": "text", "pattern": "x", "timeout_seconds": 120}]))

    wait = next(c.args[1] for c in native.call.await_args_list if c.args[1]["action"] == "wait")
    assert wait["timeout_seconds"] == 15


@pytest.mark.parametrize(
    ("steps", "problem"),
    [
        ([], "needs 'steps'"),
        ([{"action": "eval", "js": "1"}], "'eval' cannot be a step"),
        ([{"action": "upload", "selector": "input"}], "'upload' cannot be a step"),
        ([{"action": "click", "selector": "a", "profile_id": "other"}], "names profile_id"),
        ([{"action": "goto"}], "Step 1: 'goto' needs 'url'. Nothing ran."),
        ([{"action": "goto", "url": "file:///etc/passwd"}], "Step 1: Security policy blocked"),
        ([{"action": "click", "selector": "a"}] * 11, "at most 10 steps"),
        (["click"], "Step 1 is not an object"),
    ],
)
async def test_a_list_that_cannot_run_is_refused_before_any_card(settings, steps, problem) -> None:
    store = ApprovalStore()
    tool = bind_approvals(
        BrowserTool(north_settings=settings, existing_browser=_bridge()), ApprovalMode.ASK, store=store
    )
    output = await asyncio.wait_for(tool.execute(_call(steps)), 5)  # a card would wait for ever
    assert not output.success and problem in output.error
    assert not store.all(10)


async def test_steps_belong_only_to_a_steps_call(settings) -> None:
    store = ApprovalStore()
    tool = bind_approvals(
        BrowserTool(north_settings=settings, existing_browser=_bridge()), ApprovalMode.ASK, store=store
    )
    call = ToolInput(params={"action": "click", "steps": SEARCH, "profile_id": "work"})
    output = await asyncio.wait_for(tool.execute(call), 5)
    assert not output.success and "Only action='steps'" in output.error


async def _cards(tool: BrowserTool, store: ApprovalStore, call: ToolInput) -> list:
    """Make the call as an agent would, answer every card yes, and return the cards."""
    seen = []
    running = asyncio.create_task(tool.execute(call))
    while not running.done():
        for card in store.pending():
            seen.append(card)
            store.resolve(card.id, ApprovalDecision.APPROVED, decided_by="you")
        await asyncio.sleep(0.01)
    await running
    return seen


async def test_a_whole_list_is_one_card_that_shows_each_step(settings) -> None:
    store = ApprovalStore()
    native = _bridge()
    tool = bind_approvals(BrowserTool(north_settings=settings, existing_browser=native), ApprovalMode.ASK, store=store)
    steps = [{"action": "goto", "url": "https://search.example/"}, *SEARCH]

    [card] = await _cards(tool, store, _call(steps))

    assert card.title == "Browser: Work - 4 steps"
    assert "Run 4 steps in Work on search.example:" in card.message
    assert "1. Open https://search.example/" in card.message
    assert '2. Fill input[name=q] with "north"' in card.message
    assert "3. Click button[type=submit]" in card.message
    assert "4. Wait for text Results" in card.message
    assert "stops before any click or key press that would send something" in card.message
    assert native.ran == ["goto", "fill", "click", "wait", "inspect"]


async def test_a_yes_covers_only_that_exact_list(settings) -> None:
    store = ApprovalStore()
    tool = bind_approvals(
        BrowserTool(north_settings=settings, existing_browser=_bridge()), ApprovalMode.ASK, store=store
    )

    assert len(await _cards(tool, store, _call(SEARCH))) == 1
    changed = [*SEARCH[:1], {"action": "click", "selector": "button.delete"}]
    assert len(await _cards(tool, store, _call(changed))) == 1


async def test_a_list_that_only_reads_an_opened_site_needs_no_new_card(settings) -> None:
    store = ApprovalStore()
    tool = bind_approvals(
        BrowserTool(north_settings=settings, existing_browser=_bridge()), ApprovalMode.ASK, store=store
    )

    assert await _cards(
        tool,
        store,
        ToolInput(params={"action": "goto", "url": "https://search.example/", "profile_id": "work", "task_id": TASK}),
    )
    reads = [
        {"action": "goto", "url": "https://search.example/page/2"},
        {"action": "query", "selector": "h2"},
        {"action": "wait", "condition": "text", "pattern": "Next"},
    ]
    assert await _cards(tool, store, _call(reads)) == []
    assert len(await _cards(tool, store, _call([{"action": "goto", "url": "https://other.example/"}]))) == 1


async def test_in_autonomous_mode_the_decider_rules_once_for_a_list(settings) -> None:
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
    native = _bridge()
    tool = BrowserTool(north_settings=settings, existing_browser=native)
    bind_approvals(tool, ApprovalMode.AUTONOMOUS, store=store, decider=decider)

    output = await tool.execute(_call(SEARCH))

    assert output.success
    assert [action.operation for action in decider.actions] == ["steps"]
    assert native.ran == ["fill", "click", "wait", "inspect"]


async def test_a_single_click_shows_the_page_after_it(settings) -> None:
    native = _bridge()
    tool = BrowserTool(north_settings=settings, existing_browser=native)

    output = await tool.run(ToolInput(params={"action": "click", "uid": "n4", "profile_id": "work", "task_id": TASK}))

    assert native.ran == ["click", "inspect"]
    assert "### Page now: T - u" in tool.format_output(output.data)


async def test_a_read_shows_no_extra_page_view(settings) -> None:
    native = _bridge()
    tool = BrowserTool(north_settings=settings, existing_browser=native)

    await tool.run(ToolInput(params={"action": "query", "selector": "h2", "profile_id": "work", "task_id": TASK}))

    assert native.ran == ["query"]


def test_a_list_without_a_profile_needs_a_card_only_when_it_changes_something() -> None:
    tool = BrowserTool(binary_cmd=["chrome-agent"])
    assert tool.mutates({"action": "steps", "steps": [{"action": "query", "selector": "a"}]}) is False
    assert tool.mutates({"action": "steps", "steps": [{"action": "click", "selector": "a"}]}) is True


async def test_a_step_on_a_vanished_uid_is_found_again_before_north_looks_at_it(settings) -> None:
    """The check before a click then reads the element the click will land on, a "Send" included."""
    native = _bridge(target_text="Send invitation")
    order: list[str] = []
    native.relocate = AsyncMock(side_effect=lambda profile, task, uid: order.append(f"relocate {uid}") or "found")

    async def read_page(command, timeout):
        if command[0] == "inspect":  # the tree, with the uid back on its element
            return SimpleNamespace(stdout=json.dumps({"snapshot": 'uid=n4 button "Send invitation"'}))
        page = {"url": "https://www.linkedin.com/in/ada/", "title": "Ada", "focused": None, "target": None}
        return SimpleNamespace(stdout=json.dumps({"result": json.dumps(page)}))

    native.description_runner = Mock(side_effect=lambda profile, params: order.append("look") or read_page)
    tool = BrowserTool(north_settings=settings, existing_browser=native)

    output = await tool.run(_call([{"action": "click", "uid": "n4"}]))

    assert order[:2] == ["relocate n4", "look"]
    assert not output.success and "send something" in output.error  # the found-again "Send" is still a send


async def test_a_failed_click_shows_the_page(settings) -> None:
    """The model's next call after a failed click or wait was an inspect, just to see why."""
    native = _bridge(fail_on="click")
    tool = BrowserTool(north_settings=settings, existing_browser=native)

    output = await tool.run(ToolInput(params={"action": "click", "uid": "n4", "profile_id": "work", "task_id": TASK}))

    assert not output.success
    assert native.ran == ["click", "inspect"]
    assert output.data["page"]["snapshot"] == 'uid=n1 button "More"'


async def test_a_call_refused_for_its_profile_shows_no_page(settings) -> None:
    native = _bridge()
    tool = BrowserTool(north_settings=settings, existing_browser=native)

    output = await tool.run(ToolInput(params={"action": "click", "uid": "n4", "task_id": TASK}))

    assert not output.success and "page" not in output.data
    assert native.ran == []


@pytest.mark.parametrize(
    ("params", "problem"),
    [
        ({"action": "eval", "script": "document.title"}, "'eval' needs 'js'; the browser does not take 'script'"),
        ({"action": "click"}, "'click' needs 'uid' or 'selector'"),
        ({"action": "fill", "selector": "input"}, "'fill' needs 'value'"),
        ({"action": "goto"}, "'goto' needs 'url'"),
    ],
)
async def test_a_call_missing_what_it_needs_is_refused_before_any_card_and_names_it(settings, params, problem) -> None:
    """8 such calls waited for a decision first (2026-11-10 to 11-22), and then failed anyway."""
    store = ApprovalStore()
    native = _bridge()
    tool = bind_approvals(BrowserTool(north_settings=settings, existing_browser=native), ApprovalMode.ASK, store=store)

    call = ToolInput(params={**params, "profile_id": "work", "task_id": TASK})
    output = await asyncio.wait_for(tool.execute(call), 5)

    assert not output.success and output.error == f"{problem}. Nothing ran."
    assert not store.all(10) and native.ran == []
