"""A task that starts over after a model outage keeps what the user already decided in it.

The LinkedIn task asked "which Chrome profile?" after the user had already
approved one, because the retried run started from nothing.
"""

from __future__ import annotations

from approval import ApprovalDecision, Card, CardType
from tests.unit.orchestrator.test_model_scarcity import _orch


def _decided(orch, card_type: CardType, title: str, message: str, status: str, task: str = "t1", chosen: str = ""):
    card = Card.new(type=card_type, agent="general", title=title, message=message, task_id=task)
    orch._approval_store.add(card)
    orch._approval_store.resolve(card.id, status, chosen_option=chosen)


def test_a_retried_task_hears_what_was_already_decided() -> None:
    orch = _orch()
    _decided(orch, CardType.APPROVAL, "Browser: Personal", "Open linkedin.com in Personal.", ApprovalDecision.APPROVED)
    _decided(orch, CardType.QUESTION, "Question", "Which Chrome profile?", ApprovalDecision.ANSWERED, chosen="sjsu.edu")
    _decided(orch, CardType.APPROVAL, "Browser: x", "Elsewhere", ApprovalDecision.APPROVED, task="another_task")

    context = orch._with_earlier_decisions("t1", "Workspace: ~")

    assert context.startswith("Workspace: ~\n\n## Already decided in this task")
    assert "- Approved: Browser: Personal - Open linkedin.com in Personal." in context
    assert '- Asked "Which Chrome profile?" and the user answered: sjsu.edu' in context
    assert "Elsewhere" not in context
    assert context.index("Approved") < context.index("Asked")  # in the order they were decided


def test_a_first_run_or_a_still_pending_card_adds_nothing() -> None:
    orch = _orch()
    orch._approval_store.add(
        Card.new(type=CardType.QUESTION, agent="general", title="Q", message="Pending?", task_id="t1")
    )

    assert orch._with_earlier_decisions("t1", "context") == "context"
    assert orch._with_earlier_decisions("t2", "") == ""
