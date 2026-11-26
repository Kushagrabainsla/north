"""Starting the server process: what it is told about itself."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from cli import _server


def test_the_server_is_told_its_own_address_so_a_coding_agent_s_hook_reaches_it(monkeypatch, tmp_path) -> None:
    """The hook's address was always port 8000: on any other port every action the agent took was refused."""
    started: dict = {}

    class Process:
        pid = 4321

    def popen(cmd, **kwargs):
        started.update(kwargs)
        return Process()

    monkeypatch.setattr(_server.subprocess, "Popen", popen)

    _server._start_server_process(9123, workspace=str(tmp_path), host="0.0.0.0")

    assert started["env"]["NORTH_NORTH_ORCHESTRATOR_URL"] == "http://127.0.0.1:9123"
    assert started["env"]["NORTH_NORTH_WORKSPACE"] == str(tmp_path)


def test_a_server_started_from_a_deleted_folder_still_has_a_folder_its_children_can_use(monkeypatch, tmp_path) -> None:
    """The dashboard's update runs in site-packages, which `uv tool install --force` deletes and makes again.

    The new server inherited the deleted folder, so every uv it started failed with "Current directory
    does not exist", and no tool north wrote could get its packages until north was started by hand.
    """
    real_popen = subprocess.Popen
    started: dict = {}

    class Process:
        pid = 4321

    def popen(cmd, **kwargs):
        started.update(kwargs)
        return Process()

    monkeypatch.setattr(_server.subprocess, "Popen", popen)
    gone = tmp_path / "site-packages"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()

    _server._start_server_process(9123, workspace=str(tmp_path))

    child = real_popen(
        [sys.executable, "-c", "import os; print(os.getcwd())"],
        cwd=started.get("cwd"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out, err = child.communicate(timeout=30)
    assert child.returncode == 0, err
    assert (Path(out.strip()) / "orchestrator" / "app.py").is_file()
