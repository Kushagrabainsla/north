"""When a task ends for good, however it ended, its tools close what they opened for it.

Issue #76, W1: a task's browser tab stayed open until north stopped, a tab for
every task. A paused task can resume, so it keeps what it opened.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from tests.unit.orchestrator.test_orchestrator_security import _orchestrator


def _with_task(status: str | None):
    orchestrator = _orchestrator()
    orchestrator._tool_registry = MagicMock(end_task=AsyncMock())
    store = MagicMock(clear=AsyncMock())
    store.get = AsyncMock(return_value=SimpleNamespace(status=status) if status else None)
    orchestrator._running_task_store = store
    return orchestrator


async def _settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def test_a_task_that_ended_has_its_tools_close_what_they_opened() -> None:
    orchestrator = _with_task(None)

    await orchestrator._release_finished_task("t1")
    await _settle()

    orchestrator._tool_registry.end_task.assert_awaited_once_with("t1")


async def test_a_paused_task_keeps_what_it_opened() -> None:
    orchestrator = _with_task("paused")

    await orchestrator._release_finished_task("t1")
    await _settle()

    orchestrator._tool_registry.end_task.assert_not_called()


async def test_cancelling_a_paused_task_closes_what_it_opened() -> None:
    """A paused task has no pipeline left to unwind, so cancelling it is its ending."""
    orchestrator = _with_task("paused")

    assert await orchestrator.cancel_task("t1")
    await _settle()

    orchestrator._tool_registry.end_task.assert_awaited_once_with("t1")
