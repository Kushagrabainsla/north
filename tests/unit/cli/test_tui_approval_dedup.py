"""Regression test: the same approval card must prompt only ONCE in the TUI.

If the SSE event is delivered twice (two clients / reconnect), a second
approval_required for the same card_id must be ignored, not re-prompted.
"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, "/Users/kushagrabainsls/Desktop/projects/north")

from cli.tui import NorthApp


def _make_app() -> NorthApp:
    app = NorthApp(base_url="http://x", headers={}, workspace=None, yolo=False)
    # Neutralise Textual screen machinery the handler doesn't need.
    app._log = lambda *a, **k: None  # type: ignore[method-assign]
    app._log_rich = lambda *a, **k: None  # type: ignore[method-assign]
    app._set_status = lambda *a, **k: None  # type: ignore[method-assign]
    app._submit_approval = AsyncMock()  # type: ignore[method-assign]
    return app


@pytest.mark.asyncio
async def test_duplicate_card_id_does_not_reprompt():
    app = _make_app()
    card = {"card_id": "c1", "message": "turn on lamp?", "options": ["Approve", "Reject"]}

    await app._on_approval_required("t1", card)
    assert app._approval_pending is not None
    first_pending = app._approval_pending

    # Same card delivered again - must be ignored (no re-prompt).
    await app._on_approval_required("t1", card)
    assert app._approval_pending is first_pending  # unchanged, not a new prompt

    # A DIFFERENT card should still prompt (not collapsed).
    other = {"card_id": "c2", "message": "different?", "options": ["Approve", "Reject"]}
    await app._on_approval_required("t1", other)
    assert app._approval_pending is not first_pending
    assert app._approval_pending["card_id"] == "c2"


@pytest.mark.asyncio
async def test_question_card_dedup():
    app = _make_app()
    q = {"card_id": "q1", "question": "which one?", "options": ["A", "B"]}
    await app._on_question_required("t1", q)
    assert app._approval_pending is not None
    await app._on_question_required("t1", q)  # duplicate - ignored
    assert app._approval_pending["card_id"] == "q1"
