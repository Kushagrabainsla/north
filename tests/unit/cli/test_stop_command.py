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
    monkeypatch.setattr("cli.main.pairing_url", lambda url: f"{url}/app/?pair=t")

    result = runner.invoke(app, ["web", "--no-open"])
    assert result.exit_code == 0
    assert calls and calls[0]["reload"] is False


def _sleeper(*args: str):
    import subprocess
    import sys

    return subprocess.Popen([sys.executable, *args])


def _north_like(home, code: str = "import time; time.sleep(60)"):
    """A process that looks like a north server of *home*: its command line names the app, its NORTH_HOME is *home*."""
    import os
    import subprocess
    import sys

    return subprocess.Popen(
        [sys.executable, "-c", code, "orchestrator.app:app"], env={**os.environ, "NORTH_HOME": str(home)}
    )


def test_stop_server_takes_the_spawned_worker_with_the_supervisor(tmp_path, monkeypatch):
    """A reload supervisor's worker outlived it and kept serving the old code."""
    import time

    import psutil

    from cli.main import _stop_server

    child_pid = tmp_path / "child.pid"
    parent = _north_like(
        tmp_path,
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


def test_stop_server_leaves_out_the_process_doing_the_stopping(tmp_path, monkeypatch):
    """An update started from the dashboard is a child of the server, and stopped itself."""
    import os

    import psutil

    from cli.main import _stop_server

    server = _north_like(tmp_path)
    signalled: list[int] = []
    monkeypatch.setattr("config.settings.settings.north_home", tmp_path)
    (tmp_path / "north.pid").write_text(str(server.pid))
    monkeypatch.setattr("cli.main._port_in_use", lambda host, port: False)
    # This process stands in for the update: one of the server's children.
    monkeypatch.setattr(psutil.Process, "children", lambda self, recursive=False: [psutil.Process(os.getpid())])
    monkeypatch.setattr(psutil.Process, "terminate", lambda self: signalled.append(self.pid))
    monkeypatch.setattr(psutil, "wait_procs", lambda procs, timeout=None: (list(procs), []))
    try:
        _stop_server(8000)
    finally:
        server.kill()
        server.wait()

    assert signalled == [server.pid]


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


def _listening_north(home):
    """A north-like server of *home* that listens on a free port; returns the process and its port."""
    import socket
    import time

    from cli._server import _port_in_use

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    code = f"import socket, time; s = socket.socket(); s.bind(('127.0.0.1', {port})); s.listen(); time.sleep(60)"
    server = _north_like(home, code)
    for _ in range(50):
        if _port_in_use("127.0.0.1", port):
            break
        time.sleep(0.1)
    return server, port


def _two_homes(tmp_path):
    mine, other = tmp_path / "mine", tmp_path / "other"
    mine.mkdir()
    other.mkdir()
    return mine, other


def test_stopping_this_homes_north_leaves_another_homes_north_running(tmp_path, monkeypatch):
    """Stopping the user's north also stopped a benchmark's server with its own home and port (2026-11-11)."""
    from cli.main import _stop_server

    mine, other = _two_homes(tmp_path)
    my_server, my_port = _listening_north(mine)
    their_server, _ = _listening_north(other)
    monkeypatch.setattr("config.settings.settings.north_home", mine)
    (mine / "north.pid").write_text(str(my_server.pid))
    try:
        _stop_server(my_port)

        assert my_server.wait(timeout=5) is not None
        assert their_server.poll() is None, "another home's north keeps running"
    finally:
        for server in (my_server, their_server):
            server.kill()
            server.wait()


def test_a_stale_pid_file_naming_another_homes_north_stops_nothing(tmp_path, monkeypatch):
    """A pid file can outlive its server, and its number can then belong to any process."""
    from cli.main import _stop_server

    mine, other = _two_homes(tmp_path)
    their_server, _ = _listening_north(other)
    monkeypatch.setattr("config.settings.settings.north_home", mine)
    (mine / "north.pid").write_text(str(their_server.pid))
    monkeypatch.setattr("cli.main._port_in_use", lambda host, port: False)
    try:
        _stop_server(8000)

        assert their_server.poll() is None
    finally:
        their_server.kill()
        their_server.wait()


def test_stop_all_finds_only_this_homes_north(tmp_path, monkeypatch):
    """`north stop --all` matched every north on the machine, whatever its home."""
    from cli.main import _north_processes

    mine, other = _two_homes(tmp_path)
    my_server, my_port = _listening_north(mine)
    their_server, _ = _listening_north(other)
    quiet_mine = _north_like(mine)  # found by its home and command line, without listening anywhere
    monkeypatch.setattr("config.settings.settings.north_home", mine)
    try:
        found = {proc.pid for proc in _north_processes(my_port)}

        assert my_server.pid in found and quiet_mine.pid in found
        assert their_server.pid not in found
    finally:
        for server in (my_server, their_server, quiet_mine):
            server.kill()
            server.wait()


def test_a_process_that_only_mentions_north_is_not_a_north_server(tmp_path):
    """A shell or an editor that names the app is not one, even with this home in its environment."""
    import os
    import subprocess

    import psutil

    from cli._server import _is_this_homes_north

    editor = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 60 # orchestrator.app:app"], env={**os.environ, "NORTH_HOME": str(tmp_path)}
    )
    try:
        assert not _is_this_homes_north(psutil.Process(editor.pid), tmp_path)
    finally:
        editor.kill()
        editor.wait()
