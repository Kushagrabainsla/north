from __future__ import annotations

import time

from typer.testing import CliRunner

from cli.main import _render_provider_menu, app
from inference.codex_auth import CodexToken, CodexTokenStore

runner = CliRunner()


def test_provider_menu_is_generated_from_registry(capsys) -> None:
    _render_provider_menu()
    output = capsys.readouterr().out
    assert "OpenAI Codex" in output
    assert "OpenCode Zen" in output
    assert "No inference provider is configured" in output


def test_auth_status_lists_codex(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NORTH_HOME", str(tmp_path))
    result = runner.invoke(app, ["auth", "status"])
    assert result.exit_code == 0
    assert "OpenAI Codex" in result.output
    assert "Not logged in" in result.output


def test_auth_logout_removes_north_owned_token(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NORTH_HOME", str(tmp_path))
    store = CodexTokenStore()
    store.save(CodexToken("access", "refresh", time.time() + 3600, "account"))

    result = runner.invoke(app, ["auth", "logout", "openai-codex"])

    assert result.exit_code == 0
    assert not store.exists()
    assert "credentials removed" in result.output


def test_auth_login_dispatches_to_codex_flow(monkeypatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr("cli.main._login_codex_interactive", lambda *, open_browser: called.append(open_browser))

    result = runner.invoke(app, ["auth", "login", "openai-codex", "--no-browser"])

    assert result.exit_code == 0
    assert called == [False]
