"""Regression test: the same approval card must prompt only ONCE in the TUI.

If the SSE event is delivered twice (two clients / reconnect), a second
approval_required for the same card_id must be ignored, not re-prompted.
"""

import sys
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, "/Users/kushagrabainsls/Desktop/projects/north")

from cli.tui import NorthApp


def _make_app() -> NorthApp:
    app = NorthApp(base_url="http://x", headers={}, workspace=None)
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


@pytest.mark.asyncio
async def test_a_card_answered_elsewhere_no_longer_takes_your_next_prompt():
    """Answered on the phone, the card stayed pending here and the next prompt was sent to it, then dropped."""
    app = _make_app()
    app._submit_prompt = AsyncMock()  # type: ignore[method-assign]
    await app._on_question_required("t1", {"card_id": "q1", "question": "which quarter?", "options": []})

    await app._on_approval_responded("t1", {"card_id": "q1", "decision": "answered", "chosen_option": "Q3"})
    await app.on_input_submitted(_submitted(app, "now plan next quarter"))

    assert app._approval_pending is None
    app._submit_approval.assert_not_awaited()
    app._submit_prompt.assert_awaited_once_with("now plan next quarter")


@pytest.mark.asyncio
async def test_a_task_that_ended_takes_its_cards_with_it():
    app = NorthApp(base_url="http://x", headers={}, workspace=None)
    async with app.run_test(size=(100, 30)):
        await app._on_approval_required("t1", {"card_id": "c1", "message": "turn on lamp?"})

        await app._on_task_cancelled("t1", {})

        assert app._approval_pending is None


def _answering_client(app: NorthApp, status_code: int) -> list[str]:
    import contextlib

    answered: list[str] = []

    class _Client:
        async def post(self, url, json=None, **kw):
            answered.append(json["card_id"])
            return type("R", (), {"status_code": status_code})()

    @contextlib.asynccontextmanager
    async def http():
        yield _Client()

    app._http = http  # type: ignore[method-assign]
    return answered


@pytest.mark.asyncio
async def test_with_two_cards_waiting_the_first_is_still_answerable_after_the_second():
    """The second card replaced the first, which could no longer be answered from the TUI."""
    app = NorthApp(base_url="http://x", headers={}, workspace=None)
    async with app.run_test(size=(100, 30)):
        await app._on_approval_required("t1", {"card_id": "c1", "message": "first?", "options": ["Approve", "Reject"]})
        await app._on_approval_required("t2", {"card_id": "c2", "message": "second?", "options": ["Approve", "Reject"]})
        answered = _answering_client(app, 200)

        await app._submit_approval("y")
        await app._submit_approval("y")

        assert answered == ["c2", "c1"]
        assert app._approval_pending is None


@pytest.mark.asyncio
async def test_an_answer_the_server_refused_is_reported_not_swallowed():
    app = NorthApp(base_url="http://x", headers={}, workspace=None)
    async with app.run_test(size=(100, 30)):
        logged: list[str] = []
        app._log = logged.append  # type: ignore[method-assign]
        await app._on_approval_required("t1", {"card_id": "c1", "message": "lamp?", "options": ["Approve", "Reject"]})
        _answering_client(app, 409)

        await app._submit_approval("y")

        assert any("not recorded" in line for line in logged)


def _submitted(app: NorthApp, text: str):
    from types import SimpleNamespace

    app._submitted_text = lambda event: text  # type: ignore[method-assign]
    app._remember_input = lambda text: None  # type: ignore[method-assign]
    return SimpleNamespace(value=text, input=None)
