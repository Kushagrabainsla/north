"""The approval layer's one entry point, reached through `Tool.execute`.

Before it, twelve mutating tools never asked - among them `north_config`, which
could switch north to autonomous on a model's word.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from approval.approvals import Approvals, Request
from approval.models import ApprovalDecision
from approval.policy import Action, ActionKind
from approval.store import ApprovalStore
from config.approval_mode import ApprovalMode
from tests.conftest import StubDecider, bind_approvals, deciding, rejecting_store
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.specialized.north_config import NorthConfigTool
from tools.universal.browser import BrowserTool


class _Recorder(Tool):
    """A mutating tool that describes nothing itself - the default description applies."""

    name = "change_things"
    description = "Changes things."
    is_mutating = True

    def __init__(self) -> None:
        self.ran = False

    async def run(self, input: ToolInput) -> ToolOutput:
        self.ran = True
        return ToolOutput(success=True)


@pytest.mark.asyncio
async def test_a_model_cannot_switch_the_approval_mode_unasked() -> None:
    store = rejecting_store()
    tool = bind_approvals(NorthConfigTool(), store=store)

    out = await tool.execute(ToolInput(params={"action": "autonomy", "value": "autonomous"}))

    assert out.failure_kind == "refused"
    store.wait_for_decision.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_browser_click_asks_before_it_happens() -> None:
    store = rejecting_store()
    tool = bind_approvals(BrowserTool(binary_cmd=["false"]), store=store)

    out = await tool.execute(ToolInput(params={"action": "click", "selector": "#submit"}))

    assert out.failure_kind == "refused"
    store.wait_for_decision.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_tool_that_describes_nothing_is_still_gated() -> None:
    tool = bind_approvals(_Recorder(), store=rejecting_store())

    out = await tool.execute(ToolInput(params={"target": "x"}))

    assert out.failure_kind == "refused" and not tool.ran


@pytest.mark.asyncio
async def test_the_default_description_ignores_the_task_so_a_decision_replays() -> None:
    tool = _Recorder()

    first = await tool.describe(ToolInput(params={"target": "x", "task_id": "t1"}))
    second = await tool.describe(ToolInput(params={"target": "x", "task_id": "t2"}))

    assert first.action.describe() == second.action.describe()


@pytest.mark.asyncio
async def test_an_approved_call_runs_and_is_recorded_when_allowed_unasked() -> None:
    store = MagicMock()
    tool = bind_approvals(_Recorder(), ApprovalMode.YOLO, store=store)

    out = await tool.execute(ToolInput(params={"target": "x"}))

    assert out.success and tool.ran
    store.add.assert_called_once()  # something north did unasked stays visible


@pytest.mark.asyncio
async def test_unbound_lets_reading_through_and_refuses_changing() -> None:
    unbound = Approvals.unbound()
    read = Request(Action(agent="t", kind=ActionKind.OTHER, summary="r", read_only=True), "t", "m")
    change = Request(Action(agent="t", kind=ActionKind.OTHER, summary="w"), "t", "m")

    assert (await unbound.decide(read, task_id=None)).allowed
    refused = await unbound.decide(change, task_id=None)
    assert not refused.allowed and "fail closed" in refused.reason


@pytest.mark.asyncio
async def test_the_approved_request_reaches_run() -> None:
    seen: list[object] = []

    class _Keeps(_Recorder):
        async def describe(self, input: ToolInput) -> Request:
            request = await super().describe(input)
            return Request(request.action, request.title, request.message, prepared="plan")

        async def run(self, input: ToolInput) -> ToolOutput:
            seen.append(input.approved.prepared)
            return ToolOutput(success=True)

    tool = bind_approvals(_Keeps(), ApprovalMode.YOLO, store=MagicMock())

    await tool.execute(ToolInput(params={}))

    assert seen == ["plan"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("decision", "runs"), [(ApprovalDecision.APPROVED, True), (ApprovalDecision.REJECTED, False)])
async def test_autonomous_leaves_nothing_pending_and_keeps_every_reason(decision: str, runs: bool) -> None:
    """#29: in autonomous no card is ever pending, and every decision has a stored reason."""
    store = ApprovalStore()
    decider = deciding(decision, reason="fits how you work", used=("works in ~/src",))
    tool = bind_approvals(_Recorder(), ApprovalMode.AUTONOMOUS, store=store, decider=decider)

    await tool.execute(ToolInput(params={"target": "x"}))

    assert tool.ran is runs
    assert not store.pending()
    [card] = store.all()
    assert card.status == decision
    assert card.decided_by == "memory_decider"
    assert card.reason == "fits how you work"
    assert [ref.text for ref in card.memory_used] == ["works in ~/src"]


@pytest.mark.asyncio
async def test_autonomous_waits_for_you_only_when_the_decider_cannot_decide() -> None:
    store = rejecting_store()
    tool = bind_approvals(_Recorder(), ApprovalMode.AUTONOMOUS, store=store, decider=StubDecider(None))

    await tool.execute(ToolInput(params={"target": "x"}))

    store.wait_for_decision.assert_awaited_once()
