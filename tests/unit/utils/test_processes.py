"""stop_process_group ends a process and everything it started."""

from __future__ import annotations

import asyncio
import os
import signal
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


@pytest.mark.asyncio
async def test_a_group_that_has_already_exited_raises_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS says "not permitted" for a group of exited, uncollected processes; a cancel crashed on it."""
    process = await asyncio.create_subprocess_exec("sh", "-c", "sleep 30", start_new_session=True)

    def not_permitted(group: int, number: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    real = os.killpg
    monkeypatch.setattr(os, "killpg", not_permitted)
    try:
        await asyncio.wait_for(stop_process_group(process, grace_seconds=0), 10)
    finally:
        monkeypatch.setattr(os, "killpg", real)
        os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


@pytest.mark.asyncio
async def test_no_grace_stops_a_real_group_without_raising(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    process = await asyncio.create_subprocess_exec(
        "sh", "-c", f"sleep 30 & echo $! > {pid_file}; wait", start_new_session=True
    )
    child = await started_pid(pid_file)

    await stop_process_group(process, grace_seconds=0)

    assert not is_running(child)


@pytest.mark.asyncio
async def test_a_child_holding_the_output_stops_after_its_shell_has_exited(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    process = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        f"sleep 30 & echo $! > {pid_file}",
        stdout=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    child = await started_pid(pid_file)
    await process.wait()

    await stop_process_group(process, grace_seconds=1.0)

    assert not is_running(child)


@pytest.mark.asyncio
async def test_a_server_a_finished_command_left_on_purpose_keeps_running() -> None:
    process = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        "nohup sleep 30 >/dev/null 2>&1 & echo $!",
        stdout=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    output, _ = await process.communicate()
    server = int(output.strip())
    try:
        await stop_process_group(process)

        assert is_running(server)
    finally:
        os.kill(server, signal.SIGKILL)
