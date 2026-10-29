"""Cancelling a task stops the commands it started, and what they started.

Cancel only stopped north's own coroutine: bash and the browser killed their
process on a timeout and nowhere else, and each ran in a process group of its
own, so a cancelled task's command, and anything it had started, ran on.
Found by verifying north's Oct 24 self-audit.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.conftest import approving_store, bind_approvals, is_running, started_pid
from tools.models import ToolInput
from tools.specialized.bash import BashTool
from tools.universal import browser


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_a_cancelled_bash_call_stops_its_command(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    tool = bind_approvals(BashTool(), store=approving_store())
    call = asyncio.create_task(
        tool.execute(
            ToolInput(
                params={"command": f"sleep 30 & echo $! > {pid_file}; wait", "workspace": str(tmp_path)},
                granted_workspace=str(tmp_path),
            )
        )
    )
    child = await started_pid(pid_file)

    await _cancel(call)

    assert not is_running(child)


@pytest.mark.asyncio
async def test_a_cancelled_browser_command_stops_the_browser_it_started(tmp_path: Path) -> None:
    pid_file = tmp_path / "browser.pid"
    command = asyncio.create_task(
        browser._run_chrome_agent(["sh", "-c", f"sleep 30 & echo $! > {pid_file}; wait"], timeout=60)
    )
    child = await started_pid(pid_file)

    await _cancel(command)

    assert not is_running(child)
