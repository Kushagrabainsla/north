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
    monkeypatch.setattr("cli.main._stop_all_north_processes", lambda: 2)
    monkeypatch.setattr("cli.main._stop_server", lambda p: None)

    result = runner.invoke(app, ["stop", "--all"])
    assert result.exit_code == 0
    assert "stopped all north processes" in result.output.lower()
    assert "2 process(es) terminated" in result.output.lower()
