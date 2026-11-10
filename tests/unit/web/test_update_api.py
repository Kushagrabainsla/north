"""The System page's Update button: it starts `north update` on its own, once, and reports how it went."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from fastapi import HTTPException

from orchestrator.api_context import ApiServices, bind_services
from utils.update_record import LOG_NAME, last_update, record_update
from utils.version import NORTH_VERSION
from web import updates


@pytest.fixture
def home(tmp_path, monkeypatch):
    started: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        started.append(real_popen(*args, **kwargs))
        return started[-1]

    # A stand-in update that waits, so the test can see it running without updating anything.
    monkeypatch.setattr(updates, "_update_command", lambda: [sys.executable, "-c", "import time; time.sleep(30)"])
    monkeypatch.setattr(updates.subprocess, "Popen", popen)
    with bind_services(ApiServices(north_home=tmp_path)):
        yield tmp_path, started
    for process in started:
        process.kill()
        process.wait()


async def test_the_page_shows_the_running_version_and_no_update_yet(home) -> None:
    status = await updates.update_status()

    assert status["version"] == NORTH_VERSION
    assert status["last"] is None


async def test_the_button_starts_the_update_on_its_own_and_once(home) -> None:
    north_home, started = home

    assert await updates.start_update() == {"state": "running"}
    with pytest.raises(HTTPException) as second:
        await updates.start_update()

    assert second.value.status_code == 409
    assert len(started) == 1
    last = last_update(north_home)
    assert last is not None and last["state"] == "running" and last["pid"] == started[0].pid
    assert (north_home / LOG_NAME).exists()
    # Its own session: stopping the server's process group does not stop the update.
    assert os.getsid(started[0].pid) != os.getsid(0)


async def test_a_finished_update_is_shown_with_its_time_and_another_can_start(home) -> None:
    north_home, _ = home
    record_update(north_home, "failed", "Install failed:\nno network")

    status = await updates.update_status()

    assert status["last"]["state"] == "failed"
    assert status["last"]["detail"] == "Install failed:\nno network"
    assert isinstance(status["last"]["at"], str)
    assert await updates.start_update() == {"state": "running"}


def test_the_update_command_reaches_this_norths_cli() -> None:
    """Run as the button runs it, from the folder that holds north's code."""
    command = updates._update_command()
    command[command.index("--yes")] = "--help"

    result = subprocess.run(command, cwd=updates._CODE_ROOT, capture_output=True, text=True, timeout=120)

    assert result.returncode == 0, result.stderr
    assert "Update north" in result.stdout


async def test_the_update_runs_from_norths_code_not_its_home(home, monkeypatch) -> None:
    """`-m` looks in the working folder first, so a stray `cli` in north's home must not be it."""
    folders: list = []
    monkeypatch.setattr(updates.subprocess, "Popen", lambda *a, **k: folders.append(k["cwd"]) or _Stub())

    await updates.start_update()

    assert folders == [updates._CODE_ROOT]
    assert (updates._CODE_ROOT / "cli" / "main.py").is_file()


class _Stub:
    pid = os.getpid()
