"""TerminalNotifier: rendering is pure and writes go off-loop (C6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from approval.models import Card, CardType
from approval.terminal import TerminalNotifier


def _card(**overrides: object) -> Card:
    base: dict[str, object] = {
        "id": "card-1",
        "type": CardType.APPROVAL,
        "task_id": "task-1",
        "agent": "coder",
        "title": "Run command",
        "message": "delete the build directory",
    }
    base.update(overrides)
    return Card(**base)  # type: ignore[arg-type]


def test_render_includes_card_fields() -> None:
    text = TerminalNotifier._render(_card())
    assert "Run command" in text
    assert "delete the build directory" in text
    assert "task-1" in text


def test_render_lists_question_options() -> None:
    text = TerminalNotifier._render(_card(type=CardType.QUESTION, options=["yes", "no"]))
    assert "[1] yes" in text
    assert "[2] no" in text


@pytest.mark.asyncio
async def test_notify_appends_to_log_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_file = tmp_path / "north.log"
    monkeypatch.setenv("NORTH_LOG_FILE", str(log_file))

    await TerminalNotifier().notify(_card(title="Approve deploy"))

    assert "Approve deploy" in log_file.read_text(encoding="utf-8")
