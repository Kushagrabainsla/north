"""Question: does a tool shaped like `coding_agent` fit north's real tool and approval path?"""

from __future__ import annotations

import asyncio

from approval.approvals import Approvals
from approval.interaction import UserInteraction
from approval.models import ApprovalDecision
from approval.policy import ApprovalPolicy
from approval.store import ApprovalStore
from config.approval_mode import ApprovalMode
from tools.models import ToolInput
from tools.registry import ToolRegistry

from .prototypes import CodingTool, record, until

PARAMS = {"task_id": "t-run", "backend": "claude", "mode": "edit", "workspace": "/w", "task": "fix the bug"}


def _wired() -> tuple[ToolRegistry, CodingTool, ApprovalStore]:
    store = ApprovalStore()
    approvals = Approvals(ApprovalPolicy(mode_provider=lambda: ApprovalMode.ASK), UserInteraction(store))
    registry = ToolRegistry(approvals=approvals)
    tool = CodingTool()
    registry.register(tool)  # the same call orchestrator/app.py makes for BashTool
    return registry, tool, store


async def test_the_start_card_names_the_task_and_the_tool_runs_after_approval() -> None:
    registry, tool, store = _wired()

    waiting = asyncio.create_task(
        registry.get("coding_agent").execute(ToolInput(params=PARAMS, granted_workspace="/w"))
    )
    card = (await until(store.pending))[0]
    held = store.tasks_waiting_on_you()
    assert tool.ran_with is None, "nothing runs before the answer"
    store.resolve(card.id, ApprovalDecision.APPROVED)
    output = await waiting

    assert card.task_id == "t-run" and held == {"t-run"}
    assert output.success and output.data["granted"] == "/w"
    record("T1_start_card", {"title": card.title, "task_id": card.task_id, "message": card.message})


async def test_a_rejected_start_card_is_refused_and_never_runs() -> None:
    registry, tool, store = _wired()

    waiting = asyncio.create_task(
        registry.get("coding_agent").execute(ToolInput(params=PARAMS, granted_workspace="/w"))
    )
    card = (await until(store.pending))[0]
    store.resolve(card.id, ApprovalDecision.REJECTED)
    output = await waiting

    assert not output.success and output.failure_kind == "refused"
    assert tool.ran_with is None
