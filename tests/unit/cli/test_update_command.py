"""Tests for explicit remote and local North update sources."""

from __future__ import annotations

from pathlib import Path

import click
from typer.testing import CliRunner

from cli.main import _NORTH_GIT_URL, app

runner = CliRunner()


def _plain(result) -> str:
    """result.output with ANSI codes stripped.

    Typer's usage-error panel runs its message through Rich's CLI-token
    highlighter, which wraps recognised tokens (``--flags``, 'quoted
    strings') in their own colour codes whenever the environment reads as
    colour-capable - splicing escape sequences into the middle of a phrase
    like "--path requires --source local". Whether that triggers depends on
    the environment's own colour detection, not on anything this test
    controls, and it does trigger in CI while not doing so locally - so a
    literal substring check against the raw text is fragile in exactly the
    place these tests need it least. Stripping first checks the message
    that's actually there, independent of that.
    """
    return click.unstyle(result.output)


def test_update_defaults_to_remote_even_when_local_checkout_exists(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr("cli.main._find_project_root", lambda: tmp_path)
    monkeypatch.setattr("cli.main._update_from_git", lambda url, options: calls.append((url, options)))
    monkeypatch.setattr(
        "cli.main._update_local_checkout",
        lambda path, options: calls.append((str(path), options)),
    )

    result = runner.invoke(app, ["update", "--yes", "--no-restart"])

    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0][0] == _NORTH_GIT_URL


def test_update_local_uses_explicit_checkout_path(tmp_path, monkeypatch) -> None:
    checkout = tmp_path / "north checkout"
    checkout.mkdir()
    calls: list[Path] = []
    monkeypatch.setattr(
        "cli.main._update_local_checkout",
        lambda path, options: calls.append(path),
    )

    result = runner.invoke(
        app,
        ["update", "--source", "local", "--path", str(checkout), "--yes", "--no-restart"],
    )

    assert result.exit_code == 0
    assert calls == [checkout.resolve()]


def test_update_local_uses_detected_checkout_when_path_is_omitted(tmp_path, monkeypatch) -> None:
    calls: list[Path] = []
    monkeypatch.setattr("cli.main._find_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "cli.main._update_local_checkout",
        lambda path, options: calls.append(path),
    )

    result = runner.invoke(app, ["update", "--source", "local", "--yes", "--no-restart"])

    assert result.exit_code == 0
    assert calls == [tmp_path]


def test_update_rejects_path_for_remote_source(tmp_path) -> None:
    result = runner.invoke(app, ["update", "--path", str(tmp_path), "--yes", "--no-restart"])

    assert result.exit_code == 2
    assert "--path requires --source local" in _plain(result)


def test_update_rejects_unknown_source() -> None:
    result = runner.invoke(app, ["update", "--source", "archive", "--yes", "--no-restart"])

    assert result.exit_code == 2
    assert "--source must be 'remote' or 'local'" in _plain(result)


def test_local_update_installs_editable_without_pulling(tmp_path, monkeypatch) -> None:
    checkout = tmp_path / "north"
    (checkout / "agents").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("[project]\nname='north'\n", encoding="utf-8")
    commands: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **kwargs):
        commands.append(command)
        return _Result()

    monkeypatch.setattr("cli.main.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("cli.main._stop_server_if_running", lambda port: False)
    monkeypatch.setattr("cli.main._install_helper_binaries", lambda: None)
    monkeypatch.setattr("cli.main.subprocess.run", fake_run)

    result = runner.invoke(
        app,
        ["update", "--source", "local", "--path", str(checkout), "--yes", "--no-restart"],
    )

    assert result.exit_code == 0
    assert commands == [["uv", "tool", "install", "--editable", "--force", str(checkout.resolve())]]
    assert all("pull" not in command for command in commands)
