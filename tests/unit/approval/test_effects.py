"""An action that reaches someone or spends money is never repeated in one task (#33).

A paused flow step runs again when resumed. If it had already submitted an
application before it failed, the second submit must be refused, not sent.
"""

from __future__ import annotations

from pathlib import Path

from approval.approvals import Approvals, Request
from approval.effects import EffectLog
from approval.policy import Action, ActionKind
from config.approval_mode import ApprovalMode
from tests.conftest import approval_policy
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class _Submit(Tool):
    """Submits an application: something that reaches someone else."""

    name = "submit_application"
    description = "Submit."
    is_mutating = True

    def __init__(self, *, reaches_third_party: bool = True) -> None:
        self.submitted: list[str] = []
        self._reaches = reaches_third_party

    async def describe(self, input: ToolInput) -> Request:
        job = str(input.params.get("job"))
        return Request(
            action=Action(
                agent=self.name,
                kind=ActionKind.BROWSER,
                summary=f"{self.name} {job}",
                args=job,
                reaches_third_party=self._reaches,
            ),
            title="Submit",
            message=job,
        )

    async def run(self, input: ToolInput) -> ToolOutput:
        self.submitted.append(str(input.params.get("job")))
        return ToolOutput(success=True)


def _bind(tool: Tool, tmp_path: Path) -> Tool:
    tool.approvals = Approvals(approval_policy(ApprovalMode.YOLO), None, effects=EffectLog(tmp_path / "e.db"))
    return tool


async def test_the_same_submit_is_refused_the_second_time_in_one_task(tmp_path: Path) -> None:
    tool = _bind(_Submit(), tmp_path)

    first = await tool.execute(ToolInput(params={"job": "acme-123", "task_id": "run-1"}))
    again = await tool.execute(ToolInput(params={"job": "acme-123", "task_id": "run-1"}))

    assert first.success
    assert not again.success and again.failure_kind == "refused"
    assert "already done earlier in this task" in again.error
    assert tool.submitted == ["acme-123"]


async def test_a_different_task_or_a_different_submit_still_runs(tmp_path: Path) -> None:
    tool = _bind(_Submit(), tmp_path)

    await tool.execute(ToolInput(params={"job": "acme-123", "task_id": "run-1"}))
    await tool.execute(ToolInput(params={"job": "acme-123", "task_id": "run-2"}))
    await tool.execute(ToolInput(params={"job": "globex-9", "task_id": "run-1"}))

    assert tool.submitted == ["acme-123", "acme-123", "globex-9"]


async def test_the_record_survives_a_restart(tmp_path: Path) -> None:
    """A resume can come after north restarts."""
    await _bind(_Submit(), tmp_path).execute(ToolInput(params={"job": "acme-123", "task_id": "run-1"}))
    after_restart = _bind(_Submit(), tmp_path)

    again = await after_restart.execute(ToolInput(params={"job": "acme-123", "task_id": "run-1"}))

    assert not again.success and after_restart.submitted == []


async def test_a_reversible_action_may_repeat(tmp_path: Path) -> None:
    tool = _bind(_Submit(reaches_third_party=False), tmp_path)
    tool.name = "save_draft"

    await tool.execute(ToolInput(params={"job": "acme-123", "task_id": "run-1"}))
    await tool.execute(ToolInput(params={"job": "acme-123", "task_id": "run-1"}))

    assert tool.submitted == ["acme-123", "acme-123"]


async def test_a_failed_submit_is_not_recorded_so_it_can_be_retried(tmp_path: Path) -> None:
    class _Fails(_Submit):
        async def run(self, input: ToolInput) -> ToolOutput:
            self.submitted.append("tried")
            return ToolOutput(success=False, error="session expired")

    tool = _bind(_Fails(), tmp_path)

    await tool.execute(ToolInput(params={"job": "acme-123", "task_id": "run-1"}))
    await tool.execute(ToolInput(params={"job": "acme-123", "task_id": "run-1"}))

    assert tool.submitted == ["tried", "tried"]
