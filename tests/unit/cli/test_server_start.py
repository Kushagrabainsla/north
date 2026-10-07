"""Starting the server process: what it is told about itself."""

from __future__ import annotations

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
