"""A task is marked as having acted before a tool acts, so a stop in the middle cannot hide it."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.orchestrator import Orchestrator
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class _Acts(Tool):
    name = "acts"
    description = "changes something"
    is_mutating = True

    def __init__(self, *, read_only: bool | None = False, allowed: bool = True) -> None:
        self.order: list[str] = []
        self._read_only = read_only
        self._allowed = allowed

    async def describe(self, input: ToolInput):
        if self._read_only is None:
            return None
        return SimpleNamespace(action=SimpleNamespace(read_only=self._read_only), refused_hint="")

    def _approvals(self):
        return MagicMock(
            decide=AsyncMock(return_value=SimpleNamespace(allowed=self._allowed, reason="no", status=None)),
            refused_paths=MagicMock(return_value=None),
        )

    async def run(self, input: ToolInput) -> ToolOutput:
        self.order.append("acted")
        return ToolOutput(success=True, data={})


def _input(tool: _Acts, fail: bool = False) -> ToolInput:
    async def mark() -> None:
        if fail:
            raise OSError("disk full")
        tool.order.append("marked")

    return ToolInput(params={}, before_change=mark)


async def test_the_mark_comes_before_the_act() -> None:
    tool = _Acts()

    await tool.execute(_input(tool))

    assert tool.order == ["marked", "acted"]


async def test_no_act_when_the_mark_cannot_be_written() -> None:
    """Unmarked and acted is the case a restart would run a second time."""
    tool = _Acts()

    with pytest.raises(OSError, match="disk full"):
        await tool.execute(_input(tool, fail=True))

    assert tool.order == []


async def test_a_call_proved_read_only_is_not_marked() -> None:
    tool = _Acts(read_only=True)

    await tool.execute(_input(tool))

    assert tool.order == ["acted"]


async def test_a_read_is_not_marked() -> None:
    tool = _Acts()
    tool.is_mutating = False

    await tool.execute(_input(tool))

    assert tool.order == ["acted"]


async def test_a_refused_call_is_not_marked() -> None:
    tool = _Acts(allowed=False)

    await tool.execute(_input(tool))

    assert tool.order == []


# ---- what the mark stops ----


def _orchestrator(*, acted: bool) -> Orchestrator:
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._running_task_store = MagicMock(get=AsyncMock(return_value=SimpleNamespace(has_side_effects=acted)))
    orchestrator._queue_for_model_recovery = AsyncMock(return_value=True)  # type: ignore[method-assign]
    orchestrator._record_task_needs_attention = AsyncMock()  # type: ignore[method-assign]
    return orchestrator


async def test_a_task_that_acted_is_not_queued_to_run_again() -> None:
    from inference.exceptions import AllModelsRateLimitedError

    orchestrator = _orchestrator(acted=True)

    await orchestrator._report_task_failure("t1", 0.0, AllModelsRateLimitedError("No model could serve general"))

    orchestrator._queue_for_model_recovery.assert_not_awaited()
    assert "already acted" in orchestrator._record_task_needs_attention.await_args.args[1]


async def test_a_task_that_did_nothing_waits_for_a_model() -> None:
    from inference.exceptions import AllModelsRateLimitedError

    orchestrator = _orchestrator(acted=False)

    await orchestrator._report_task_failure("t1", 0.0, AllModelsRateLimitedError("No model could serve general"))

    orchestrator._queue_for_model_recovery.assert_awaited_once()
    orchestrator._record_task_needs_attention.assert_not_awaited()
