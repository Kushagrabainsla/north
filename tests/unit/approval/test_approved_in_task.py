"""An exact action a person approved is not asked about again in the same task.

In the issue #76 test (W4) "turn off the living room lights" raised a card, the
approved call failed ("No Kasa devices found"), and the agent that took over
called `kasa off` on "Living room lights" and raised a second, identical card,
three times out of three on Haiku.
"""

from __future__ import annotations

import asyncio

from approval.approvals import Request
from approval.models import ApprovalDecision
from approval.policy import Action, ActionKind
from approval.store import ApprovalStore
from config.approval_mode import ApprovalMode
from tests.conftest import approvals
from tools.models import ToolInput
from tools.specialized.kasa_tool import KasaTool


def _lights_off(**facts) -> Request:
    action = Action(agent="kasa", kind=ActionKind.DEVICE, summary="off lights", operation="off", args="lights", **facts)
    return Request(action, "Device Control - Approval Required", "`off` on **lights**")


async def _approve_next_card(store: ApprovalStore) -> None:
    while not store.pending():
        await asyncio.sleep(0.01)
    store.resolve(store.pending()[0].id, ApprovalDecision.APPROVED, decided_by="you")


async def _approved_once(layer, store: ApprovalStore, request: Request, task_id: str) -> None:
    answering = asyncio.create_task(_approve_next_card(store))
    assert (await layer.decide(request, task_id=task_id)).allowed
    await answering


async def test_the_same_action_later_in_the_task_is_not_asked_again() -> None:
    store = ApprovalStore()
    layer = approvals(ApprovalMode.ASK, store=store)
    await _approved_once(layer, store, _lights_off(), "t1")

    decision = await asyncio.wait_for(layer.decide(_lights_off(), task_id="t1"), timeout=2)

    assert decision.allowed and "earlier in this task" in decision.reason
    assert sorted(card.decided_by for card in store.all()) == ["replay", "you"]  # one person's answer


async def test_another_task_is_asked() -> None:
    store = ApprovalStore()
    layer = approvals(ApprovalMode.ASK, store=store)
    await _approved_once(layer, store, _lights_off(), "t1")

    asking = asyncio.create_task(layer.decide(_lights_off(), task_id="t2"))
    await asyncio.sleep(0.05)

    assert len(store.pending()) == 1
    store.resolve(store.pending()[0].id, ApprovalDecision.REJECTED)
    assert not (await asking).allowed


async def test_a_send_is_asked_about_every_time() -> None:
    store = ApprovalStore()
    layer = approvals(ApprovalMode.ASK, store=store)
    send = _lights_off(reaches_third_party=True)
    await _approved_once(layer, store, send, "t1")

    asking = asyncio.create_task(layer.decide(send, task_id="t1"))
    await asyncio.sleep(0.05)

    assert len(store.pending()) == 1
    store.resolve(store.pending()[0].id, ApprovalDecision.REJECTED)
    await asking


async def test_a_device_named_in_another_case_is_the_same_action() -> None:
    kasa = KasaTool()
    lower = await kasa.describe(ToolInput(params={"action": "off", "device": "living room lights"}))
    title = await kasa.describe(ToolInput(params={"action": "off", "device": "Living room lights"}))

    assert lower is not None and title is not None
    assert lower.action.describe() == title.action.describe()
    assert "Living room lights" in title.message
