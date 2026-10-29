"""stop_process_group ends a process and everything it started."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.conftest import is_running, started_pid
from utils.processes import stop_process_group


@pytest.mark.asyncio
async def test_stops_the_process_and_what_it_started(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    process = await asyncio.create_subprocess_exec(
        "sh", "-c", f"sleep 30 & echo $! > {pid_file}; wait", start_new_session=True
    )
    child = await started_pid(pid_file)

    await stop_process_group(process, grace_seconds=1.0)

    assert process.returncode is not None
    assert not is_running(child)


@pytest.mark.asyncio
async def test_kills_a_group_that_ignores_the_polite_signal(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    process = await asyncio.create_subprocess_exec(
        "sh", "-c", f"trap '' TERM; echo $$ > {pid_file}; while :; do sleep 0.1; done", start_new_session=True
    )
    await started_pid(pid_file)

    await stop_process_group(process, grace_seconds=0.2)

    assert process.returncode is not None


@pytest.mark.asyncio
async def test_a_finished_process_is_left_as_it_is() -> None:
    process = await asyncio.create_subprocess_exec("true", start_new_session=True)
    await process.wait()

    await stop_process_group(process)

    assert process.returncode == 0
