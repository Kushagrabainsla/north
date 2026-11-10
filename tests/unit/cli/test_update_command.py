"""Tests for explicit remote and local North update sources."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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


class _Install:
    def __init__(self, returncode: int, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = stderr


def _remote_update(monkeypatch, *, running: bool, install: _Install) -> list[int]:
    """A remote update with nothing real behind it. Returns the ports north was started on."""
    started: list[int] = []
    monkeypatch.setattr("cli.main.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("cli.main._stop_server_if_running", lambda port: running)
    monkeypatch.setattr("cli.main._install_helper_binaries", lambda: None)
    monkeypatch.setattr("cli.main.subprocess.run", lambda command, **kwargs: install)
    monkeypatch.setattr(
        "cli.main._start_server_process", lambda port, *a, **k: started.append(port) or SimpleNamespace(pid=1)
    )
    monkeypatch.setattr("cli.main._wait_for_server", lambda *a, **k: None)
    return started


def test_an_update_that_worked_is_recorded_for_the_dashboard(monkeypatch) -> None:
    from config.settings import settings
    from utils.update_record import last_update

    started = _remote_update(monkeypatch, running=True, install=_Install(0))

    result = runner.invoke(app, ["update", "--yes", "--port", "8123"])

    assert result.exit_code == 0, result.output
    assert started == [8123]
    assert last_update(settings.north_home)["state"] == "updated"


def test_a_failed_install_starts_north_again_and_says_why(monkeypatch) -> None:
    """North was stopped for the install. Left down, the dashboard that asked could not say why."""
    from config.settings import settings
    from utils.update_record import last_update

    started = _remote_update(monkeypatch, running=True, install=_Install(1, "no network"))

    result = runner.invoke(app, ["update", "--yes", "--port", "8123"])

    assert result.exit_code == 1
    assert started == [8123]
    last = last_update(settings.north_home)
    assert last["state"] == "failed" and "no network" in last["detail"]


def test_a_failed_install_leaves_a_stopped_north_stopped(monkeypatch) -> None:
    started = _remote_update(monkeypatch, running=False, install=_Install(1, "no network"))

    result = runner.invoke(app, ["update", "--yes", "--port", "8123"])

    assert result.exit_code == 1
    assert started == []


def test_yes_answers_every_question_so_nothing_waits_on_a_terminal(monkeypatch) -> None:
    """The dashboard runs the update with no terminal, so a question there would hang it."""
    started = _remote_update(monkeypatch, running=False, install=_Install(0))

    result = runner.invoke(app, ["update", "--yes", "--port", "8123"], input="")

    assert result.exit_code == 0, result.output
    assert "Start north now?" not in result.output
    assert started == [8123]


def test_declining_the_update_leaves_north_running_and_is_recorded(monkeypatch) -> None:
    """North used to be stopped before the question, so a "no" left it stopped."""
    from config.settings import settings
    from utils.update_record import last_update

    _remote_update(monkeypatch, running=True, install=_Install(0))
    stops: list[int] = []
    monkeypatch.setattr("cli.main._stop_server_if_running", lambda port: stops.append(port) or True)

    result = runner.invoke(app, ["update", "--port", "8123"], input="n\n")

    assert result.exit_code == 1
    assert stops == []
    assert last_update(settings.north_home)["state"] == "cancelled"
