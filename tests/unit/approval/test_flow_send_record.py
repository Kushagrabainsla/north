"""A flow is told when an earlier run already did the same send (issue #82, item 4).

Each scheduled run of a flow is a new task, so the per-task record (test_effects.py)
never saw the earlier runs: the outreach flow picked one person in three batches,
and only the site's "Pending" label stopped a second invitation. Now a send is also
recorded for the flow, and a later run about to repeat it is not refused - a flow
may repeat an action on purpose - but the date is stated as a fact, on the card and
to the memory decider.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from approval.approvals import Approvals, Request
from approval.decider import _action_facts
from approval.effects import EffectLog
from approval.models import ApprovalDecision
from approval.policy import Action, ActionKind
from config.approval_mode import ApprovalMode
from flows.store import FlowRunStore
from tests.conftest import approval_policy, deciding

_FLOWS = {"run-1": "outreach", "run-2": "outreach", "run-3": "news", "chat": ""}


class _Cards:
    """The card channel: keeps every card it is shown and approves it."""

    def __init__(self) -> None:
        self.shown = []

    async def ask_person(self, card, *, event):
        self.shown.append(card)
        return card.model_copy(update={"status": ApprovalDecision.APPROVED})

    def record_resolved(self, card, answer) -> None:
        return None


def _invite(person: str = "https://www.linkedin.com/in/ada") -> Request:
    return Request(
        action=Action(
            agent="browser",
            kind=ActionKind.BROWSER,
            summary=f"Send a connection request to {person}",
            operation="click",
            place=person,
            reaches_third_party=True,
        ),
        title="Send?",
        message="Send the invitation.",
    )


def _layer(tmp_path: Path, mode: ApprovalMode = ApprovalMode.ASK, decider=None) -> tuple[Approvals, _Cards]:
    cards = _Cards()
    layer = Approvals(
        approval_policy(mode, decider=decider),
        cards,  # type: ignore[arg-type]
        effects=EffectLog(tmp_path / "e.db"),
        flow_of=_FLOWS.get,
    )
    return layer, cards


async def _send(layer: Approvals, task_id: str, request: Request | None = None) -> None:
    request = request or _invite()
    decision = await layer.decide(request, task_id=task_id)
    assert decision.allowed
    layer.completed(request, task_id)


async def test_a_later_run_of_the_flow_is_told_it_sent_this_before(tmp_path) -> None:
    layer, cards = _layer(tmp_path)
    await _send(layer, "run-1")

    await _send(layer, "run-2")

    first, second = cards.shown
    assert "earlier run of this flow" not in first.message
    assert "An earlier run of this flow already did this exact action, on " in second.message


async def test_another_person_another_flow_or_a_chat_is_not_flagged(tmp_path) -> None:
    layer, cards = _layer(tmp_path)
    await _send(layer, "run-1")

    await _send(layer, "run-2", _invite("https://www.linkedin.com/in/grace"))
    await _send(layer, "run-3")
    await _send(layer, "chat")

    assert all("earlier run of this flow" not in card.message for card in cards.shown)


async def test_within_one_run_the_repeat_is_still_refused(tmp_path) -> None:
    layer, _ = _layer(tmp_path)
    await _send(layer, "run-1")

    again = await layer.decide(_invite(), task_id="run-1")

    assert not again.allowed
    assert "already done earlier in this task" in again.reason


async def test_in_autonomous_the_decider_is_told_and_decides(tmp_path) -> None:
    """No approval rule changes: the decider still decides, with the earlier send as a fact."""
    decider = deciding("approved")
    layer, cards = _layer(tmp_path, ApprovalMode.AUTONOMOUS, decider)
    await _send(layer, "run-1")

    await _send(layer, "run-2")

    told = [action.done_before for action in decider.asked]
    assert told[0] == "" and told[1] != ""
    assert cards.shown == []


def test_the_decider_reads_the_earlier_send_as_a_fact() -> None:
    action = replace(_invite().action, done_before="Nov 17, 09:00")

    assert "an earlier run of this flow already did this exact action, on Nov 17, 09:00" in _action_facts(action)


def test_a_steps_task_id_names_its_flow(tmp_path) -> None:
    store = FlowRunStore(tmp_path / "runs.db")
    with_task = store.create(run_id="r1", flow_name="outreach", task_id="task-9", inputs={})
    without = store.create(run_id="r2", flow_name="news", inputs={})

    assert store.flow_of(with_task.task_id) == "outreach"
    assert store.flow_of(f"flow:{without.run_id}") == "news"
    assert store.flow_of("not-a-step") == ""
    assert store.flow_of("") == ""
