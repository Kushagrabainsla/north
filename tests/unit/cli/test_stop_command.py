"""Unit tests for the 'north stop' and 'north stop --all' CLI command."""

from __future__ import annotations

from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


def test_stop_command_default_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.settings.north_home", tmp_path)
    monkeypatch.setattr("cli.main._port_in_use", lambda h, p: False)

    result = runner.invoke(app, ["stop"])
    assert result.exit_code == 0
    assert "not running" in result.output.lower()


def test_stop_command_with_all_flag(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.settings.north_home", tmp_path)
    monkeypatch.setattr("cli.main._stop_all_north_processes", lambda port=8000: 2)
    monkeypatch.setattr("cli.main._stop_server", lambda p: None)
    monkeypatch.setattr("cli.main._port_in_use", lambda host, port: False)

    result = runner.invoke(app, ["stop", "--all"])
    assert result.exit_code == 0
    assert "stopped all north processes" in result.output.lower()
    assert "2 terminated" in result.output.lower()


def test_stop_all_reports_nothing_running_rather_than_success(tmp_path, monkeypatch):
    """A stop that stopped nothing is not a success, and said so in green."""
    monkeypatch.setattr("config.settings.settings.north_home", tmp_path)
    monkeypatch.setattr("cli.main._stop_all_north_processes", lambda port=8000: 0)
    monkeypatch.setattr("cli.main._stop_server", lambda p: None)
    monkeypatch.setattr("cli.main._port_in_use", lambda host, port: False)

    result = runner.invoke(app, ["stop", "--all"])
    assert result.exit_code == 0
    assert "not running" in result.output.lower()


def test_stop_all_fails_when_something_still_holds_the_port(tmp_path, monkeypatch):
    """The real failure: a survivor kept serving, and went on to serve stale code.

    north's server runs as a multiprocessing spawn child whose argv matches none
    of the command-line keywords, so it survived `north stop --all` twice while
    each run printed a green tick.
    """
    monkeypatch.setattr("config.settings.settings.north_home", tmp_path)
    monkeypatch.setattr("cli.main._stop_all_north_processes", lambda port=8000: 0)
    monkeypatch.setattr("cli.main._stop_server", lambda p: None)
    monkeypatch.setattr("cli.main._port_in_use", lambda host, port: True)

    result = runner.invoke(app, ["stop", "--all"])
    assert result.exit_code == 1
    assert "still listening" in result.output.lower()


def test_plain_stop_fails_when_something_still_holds_the_port(tmp_path, monkeypatch):
    """`north stop` printed a green tick over a reload worker that kept serving."""
    monkeypatch.setattr("config.settings.settings.north_home", tmp_path)
    (tmp_path / "north.pid").write_text("1")
    monkeypatch.setattr("cli.main._stop_server", lambda p: None)
    monkeypatch.setattr("cli.main._port_in_use", lambda host, port: True)

    result = runner.invoke(app, ["stop"])
    assert result.exit_code == 1
    assert "still listening" in result.output.lower()


def test_web_starts_the_server_without_auto_reload(monkeypatch):
    """Called as a function, `start` got typer's truthy OptionInfo for reload."""
    calls = []
    monkeypatch.setattr("cli.main._ensure_web_build", lambda: None)
    monkeypatch.setattr("cli.main._port_in_use", lambda host, port: False)
    monkeypatch.setattr("cli.main.start", lambda **kwargs: calls.append(kwargs))

    result = runner.invoke(app, ["web", "--no-open"])
    assert result.exit_code == 0
    assert calls and calls[0]["reload"] is False


def _sleeper(*args: str):
    import subprocess
    import sys

    return subprocess.Popen([sys.executable, *args])


def test_stop_server_takes_the_spawned_worker_with_the_supervisor(tmp_path, monkeypatch):
    """A reload supervisor's worker outlived it and kept serving the old code."""
    import time

    import psutil

    from cli.main import _stop_server

    child_pid = tmp_path / "child.pid"
    parent = _sleeper(
        "-c",
        "import subprocess, sys, time;"
        f"c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        f"open({str(child_pid)!r}, 'w').write(str(c.pid)); time.sleep(60)",
    )
    for _ in range(50):
        if child_pid.exists() and child_pid.read_text():
            break
        time.sleep(0.1)
    worker = psutil.Process(int(child_pid.read_text()))
    monkeypatch.setattr("config.settings.settings.north_home", tmp_path)
    (tmp_path / "north.pid").write_text(str(parent.pid))
    monkeypatch.setattr("cli.main._port_in_use", lambda host, port: False)

    _stop_server(8000)

    assert parent.wait(timeout=5) is not None
    assert not worker.is_running() or worker.status() == psutil.STATUS_ZOMBIE
    assert not (tmp_path / "north.pid").exists()


def test_kill_port_finds_the_listener_without_root():
    """psutil.net_connections() needs root on macOS; the old lookup failed silently."""
    import socket
    import time

    from cli._server import _kill_port, _port_in_use

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = _sleeper(
        "-c",
        f"import socket, time; s = socket.socket(); s.bind(('127.0.0.1', {port})); s.listen(); time.sleep(60)",
    )
    for _ in range(50):
        if _port_in_use("127.0.0.1", port):
            break
        time.sleep(0.1)

    assert _kill_port("127.0.0.1", port)
    assert server.wait(timeout=5) is not None
