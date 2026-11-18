"""Tests for the Tool ABC."""

from __future__ import annotations

import pytest

from tools import Tool, ToolInput, ToolOutput


class _NoopTool(Tool):
    name = "noop"
    description = "Returns the input back as output for testing."

    async def run(self, input: ToolInput) -> ToolOutput:
        return ToolOutput(success=True, data=input.params)


def test_tool_cannot_be_instantiated_without_run() -> None:
    class _Incomplete(Tool):
        name = "broken"
        description = "no run() method"

    with pytest.raises(TypeError):
        _Incomplete()  # type: ignore[abstract]


async def test_concrete_tool_runs_and_returns_output() -> None:
    tool = _NoopTool()
    result = await tool.run(ToolInput(params={"x": 1}))
    assert result.success is True
    assert result.data == {"x": 1}


async def test_a_call_that_changes_something_times_its_prepare_decide_and_run_steps() -> None:
    """Issue #83: a slow call must show whether describing, deciding or acting took the time."""
    from approval.approvals import Approvals
    from approval.policy import ApprovalPolicy
    from config.approval_mode import ApprovalMode
    from utils.spans import collecting

    class _Changing(_NoopTool):
        is_mutating = True

    tool = _Changing()
    tool.approvals = Approvals(ApprovalPolicy(mode_provider=lambda: ApprovalMode.YOLO), None)
    with collecting() as spans:
        result = await tool.execute(ToolInput(params={"task_id": "t1"}))
    assert result.success
    assert [name for name, _ in spans] == ["prepare", "decide", "run"]


async def test_a_read_times_its_prepare_and_run_steps() -> None:
    from utils.spans import collecting

    with collecting() as spans:
        await _NoopTool().execute(ToolInput(params={}))
    assert [name for name, _ in spans] == ["prepare", "run"]
