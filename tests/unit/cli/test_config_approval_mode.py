"""`north config set approval_mode` changes the mode north runs under.

It wrote NORTH_APPROVAL_MODE to .env, but north keeps its mode in settings.json,
which overrides .env. Once Setup or any surface had saved a mode, the command
reported success and changed nothing.
"""

from __future__ import annotations

from typer.testing import CliRunner

import cli.main as main
from config.approval_mode import ApprovalMode
from config.settings import settings
from config.strategy import NorthSettings

runner = CliRunner()


def _env() -> str:
    path = settings.north_home / ".env"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_set_goes_to_the_running_north_not_the_env_file(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(main, "_api", lambda method, path, **kwargs: calls.append((method, path, kwargs.get("json"))))

    result = runner.invoke(main.app, ["config", "set", "approval_mode", "safe"])

    assert result.exit_code == 0, result.output
    assert calls == [("POST", "/orchestrator/settings", {"autonomy": "safe"})]
    assert "NORTH_APPROVAL_MODE" not in _env()


def test_a_mode_north_does_not_have_is_refused_before_anything_is_sent(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(main, "_api", lambda *args, **kwargs: calls.append(args))

    result = runner.invoke(main.app, ["config", "set", "approval_mode", "auto"])

    assert result.exit_code == 1
    assert calls == []
    assert _env() == ""


def test_get_shows_the_mode_saved_in_settings_json() -> None:
    settings.north_home.mkdir(parents=True, exist_ok=True)
    NorthSettings(settings.north_home / "settings.json").set_autonomy(ApprovalMode.AUTONOMOUS)

    result = runner.invoke(main.app, ["config", "get", "approval_mode"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "approval_mode = autonomous"
