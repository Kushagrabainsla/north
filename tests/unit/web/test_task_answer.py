"""The reply a session turn shows: an answer, or how the task ended, never any other entry."""

from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from web.api import _task_answer


def _entry(action: str, output: str, **fields) -> LedgerEntry:
    return LedgerEntry.new(source=LedgerSource.SYSTEM, task_id="t1", action=action, output=output, **fields)


def _model_call(component: str) -> LedgerEntry:
    return LedgerEntry.new(
        source=LedgerSource.INFERENCE_ROUTER,
        task_id="t1",
        action="inference_call",
        output=component,
        status=LedgerStatus.COMPLETED,
    )


def test_a_task_with_no_answer_yet_shows_no_reply() -> None:
    # Each model call writes its purpose as output: the turn showed "general", then "approval_decider".
    entries = [_entry("classified", "a plain request"), _model_call("general"), _model_call("approval_decider")]

    assert _task_answer(entries) == ("", "working")


def test_the_newest_answer_wins_with_its_outcome() -> None:
    entries = [
        _entry("agent_completed", "first answer"),
        _entry("task_synthesis", "combined answer", agent_output={"outcome_status": "waiting_for_user"}),
        _model_call("critic"),
    ]

    assert _task_answer(entries) == ("combined answer", "waiting_for_user")


def test_a_task_that_ended_without_an_answer_says_how_it_ended() -> None:
    entries = [_model_call("general"), _entry("task_failed", "Codex hit its rate limit"), _model_call("critic")]

    assert _task_answer(entries) == ("Codex hit its rate limit", "working")
