"""`north context edit` saves through North; it used to edit a file North no longer reads."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

import cli.main as main

runner = CliRunner()


class _Reply:
    def __init__(self, payload) -> None:
        self._payload = payload

    def json(self):
        return self._payload


def _fake_api(sent: list):
    def api(method, path, **kwargs):
        sent.append((method, path, kwargs.get("json")))
        return _Reply({"content": "User wants a frontier-lab role.\n"})

    return api


def _editor(change):
    def call(command):
        draft = Path(command[1])
        draft.write_text(change(draft.read_text(encoding="utf-8")), encoding="utf-8")
        return 0

    return call


def test_an_edit_is_saved_back_to_north(monkeypatch) -> None:
    sent: list = []
    monkeypatch.setattr(main, "_api", _fake_api(sent))
    monkeypatch.setattr(main.subprocess, "call", _editor(lambda text: text + "User wants to lead a team by 2028.\n"))

    result = runner.invoke(main.app, ["context", "edit", "north_stars"])

    assert result.exit_code == 0, result.output
    assert sent[-1] == (
        "PUT",
        "/orchestrator/context/north_stars",
        {"content": "User wants a frontier-lab role.\nUser wants to lead a team by 2028.\n"},
    )


def test_an_unchanged_document_is_not_written(monkeypatch) -> None:
    sent: list = []
    monkeypatch.setattr(main, "_api", _fake_api(sent))
    monkeypatch.setattr(main.subprocess, "call", _editor(lambda text: text))

    result = runner.invoke(main.app, ["context", "edit", "judgement_rules"])

    assert result.exit_code == 0 and "No changes." in result.output
    assert [method for method, _, _ in sent] == ["GET"]


def test_the_profile_is_not_editable(monkeypatch) -> None:
    sent: list = []
    monkeypatch.setattr(main, "_api", _fake_api(sent))

    result = runner.invoke(main.app, ["context", "edit", "user"])

    assert result.exit_code == 1 and sent == []
