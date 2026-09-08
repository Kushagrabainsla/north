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
