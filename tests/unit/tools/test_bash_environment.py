"""A command north runs gets north's environment without north's own credentials (#40)."""

from __future__ import annotations

import os

import pytest

from tests.conftest import bind_approvals, rejecting_store
from tools.models import ToolInput
from tools.specialized import _os_sandbox
from tools.specialized.bash import BashTool, _environment, credential_names

SAVED = {
    "NORTH_ANTHROPIC_API_KEY": "sk-saved-from-the-dashboard",
    "NORTH_TELEGRAM_BOT_TOKEN": "123:telegram",
    "NORTH_SECRET": "the-shared-secret",
}


def test_north_knows_its_credential_names() -> None:
    names = credential_names()

    assert set(SAVED) <= names
    assert {"NORTH_GROQ_API_KEY", "NORTH_OPENROUTER_API_KEY", "NORTH_GEMINI_API_KEY"} <= names
    assert "NORTH_HOME" not in names and "PATH" not in names


def test_a_saved_key_and_the_secret_are_not_in_a_commands_environment(monkeypatch) -> None:
    for name, value in SAVED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("MY_OWN_SETTING", "kept")

    environment = _environment()

    assert not set(SAVED) & set(environment)
    assert environment["MY_OWN_SETTING"] == "kept", "the user's own environment is not north's to remove"
    assert environment["PATH"].endswith(os.environ["PATH"])


@pytest.mark.skipif(_os_sandbox.current() is None, reason="needs macOS Seatbelt")
async def test_a_command_that_needs_no_card_and_prints_the_environment_shows_none_of_them(
    monkeypatch, tmp_path
) -> None:
    for name, value in SAVED.items():
        monkeypatch.setenv(name, value)
    tool = bind_approvals(BashTool(os_sandbox=True), store=rejecting_store())  # nobody may be asked

    result = await tool.execute(ToolInput(params={"command": "env", "workspace": str(tmp_path)}))

    printed = result.data["stdout"]
    assert result.success and "PATH=" in printed
    assert not any(value in printed for value in SAVED.values())
