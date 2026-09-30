"""Who decided a card, and overruling a decision north took for you (#30).

Every decided card names who decided it. You can overrule any decision north
took: the card keeps both, memory learns yours, and nothing re-runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from approval.approval_memory import ApprovalMemory
from approval.interaction import UserInteraction
from approval.models import ApprovalDecision, Card, CardType, DecidedBy, MemoryKind, MemoryRef
from approval.policy import Answer
from approval.store import ApprovalStore
from ledger import LedgerSource
from orchestrator.orchestrator import Orchestrator

_USED = (MemoryRef(kind=MemoryKind.FACT, text="you never deploy on Fridays"),)


def _decided_by_north(store: ApprovalStore, card_type: CardType = CardType.APPROVAL) -> Card:
    """A card the memory decider rejected (or answered), recorded as the approval layer records it."""
    card = Card.new(
        type=card_type,
        task_id="t1",
        agent="bash",
        title="Deploy",
        message="npm run deploy",
        options=["Approve", "Reject"] if card_type is CardType.APPROVAL else ["Postgres", "SQLite"],
        action_key="bash shell_command cmd=npm run deploy",
    )
    if card_type is CardType.APPROVAL:
        answer = Answer(ApprovalDecision.REJECTED, "", "it is Friday", DecidedBy.MEMORY_DECIDER, _USED)
    else:
        answer = Answer(ApprovalDecision.ANSWERED, "SQLite", "small project", DecidedBy.MEMORY_DECIDER, _USED)
    UserInteraction(store).record_resolved(card, answer)
    return store.get(card.id)


def _orchestrator(store: ApprovalStore, memory: ApprovalMemory | None = None) -> Orchestrator:
    ledger = MagicMock()
    ledger.write = AsyncMock()
    stream = MagicMock()
    stream.emit = AsyncMock()
    return Orchestrator(
        ledger=ledger,
        agent_registry=MagicMock(),
        north_star_checker=MagicMock(),
        execution_planner=MagicMock(),
        task_context_store=MagicMock(),
        failure_handler=MagicMock(),
        interaction=UserInteraction(store, stream_manager=stream),
        stream_manager=stream,
        approval_store=store,
        approval_memory=memory,
    )


def test_a_card_north_decided_names_who_decided_and_why() -> None:
    card = _decided_by_north(ApprovalStore())

    assert card.decided_by == DecidedBy.MEMORY_DECIDER
    assert card.reason == "it is Friday"
    assert card.memory_used == list(_USED)


async def test_a_card_you_answer_is_decided_by_you() -> None:
    store = ApprovalStore()
    card = Card.new(type=CardType.APPROVAL, task_id="t1", agent="bash", title="Deploy", message="npm run deploy")
    store.add(card)

    await _orchestrator(store).respond_approval(card.id, ApprovalDecision.APPROVED, "Approve")

    assert store.get(card.id).decided_by == DecidedBy.YOU


def test_overruling_keeps_north_decision_beside_yours(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path / "cards.db")
    card = _decided_by_north(store)

    store.overrule(card.id, ApprovalDecision.APPROVED, chosen_option="Approve", reason="the freeze is over")

    reloaded = ApprovalStore(tmp_path / "cards.db").get(card.id)
    assert (reloaded.status, reloaded.decided_by, reloaded.reason) == ("approved", "you", "the freeze is over")
    assert reloaded.overruled.status == "rejected"
    assert reloaded.overruled.decided_by == DecidedBy.MEMORY_DECIDER
    assert reloaded.overruled.reason == "it is Friday"
    assert reloaded.overruled.memory_used == list(_USED)


@pytest.mark.parametrize("decided_by", ["", DecidedBy.YOU])
def test_only_a_decision_north_took_can_be_overruled(decided_by: str) -> None:
    store = ApprovalStore()
    card = Card.new(type=CardType.APPROVAL, task_id="t1", agent="bash", title="x", message="x")
    store.add(card)
    store.resolve(card.id, ApprovalDecision.TASK_ENDED, decided_by=decided_by)

    with pytest.raises(ValueError):
        store.overrule(card.id, ApprovalDecision.APPROVED)


def test_a_pending_card_is_answered_not_overruled() -> None:
    store = ApprovalStore()
    card = Card.new(type=CardType.APPROVAL, task_id="t1", agent="bash", title="x", message="x")
    store.add(card)

    with pytest.raises(ValueError):
        store.overrule(card.id, ApprovalDecision.APPROVED)


@pytest.mark.parametrize(
    ("card_type", "status", "chosen"),
    [
        (CardType.APPROVAL, ApprovalDecision.REJECTED, ""),  # what north already decided
        (CardType.APPROVAL, ApprovalDecision.ANSWERED, "x"),  # not a verdict
        (CardType.QUESTION, ApprovalDecision.ANSWERED, ""),  # no answer
        (CardType.QUESTION, ApprovalDecision.ANSWERED, "sqlite"),  # north's answer again
    ],
)
def test_an_overrule_must_be_a_different_decision_that_fits(card_type: CardType, status: str, chosen: str) -> None:
    store = ApprovalStore()
    card = _decided_by_north(store, card_type)

    with pytest.raises(ValueError):
        store.overrule(card.id, status, chosen_option=chosen)


def test_an_unknown_card_cannot_be_overruled() -> None:
    with pytest.raises(LookupError):
        ApprovalStore().overrule("nope", ApprovalDecision.APPROVED)


async def test_overruling_an_action_teaches_the_replay_memory(tmp_path: Path) -> None:
    store = ApprovalStore()
    memory = ApprovalMemory(tmp_path / "memory.db")
    card = _decided_by_north(store)

    await _orchestrator(store, memory).overrule_approval(card.id, ApprovalDecision.APPROVED, "Approve")

    assert memory.recall(card.agent, card.action_key) == "approved"


async def test_overruling_an_answer_is_learned_like_an_answer_you_gave() -> None:
    store = ApprovalStore()
    orch = _orchestrator(store)
    card = _decided_by_north(store, CardType.QUESTION)

    await orch.overrule_approval(card.id, ApprovalDecision.ANSWERED, "Postgres")

    entry = orch._ledger.write.call_args[0][0]
    assert entry.source is LedgerSource.CLARIFICATION
    assert "The user answered: Postgres" in entry.input


async def test_overruling_runs_nothing_again() -> None:
    """What north did, or refused, stays done: no waiter wakes, no next step runs."""
    continuations = MagicMock()
    store = ApprovalStore(continuations=continuations)
    card = _decided_by_north(store)
    continuations.reset_mock()

    await _orchestrator(store).overrule_approval(card.id, ApprovalDecision.APPROVED, "Approve")

    continuations.dispatch.assert_not_called()


def test_cards_stored_with_plain_memory_strings_still_load() -> None:
    """#29 stored memory as "kind: text" strings for a few hours."""
    payload = json.loads(Card.new(type=CardType.APPROVAL, agent="bash", title="x", message="x").model_dump_json()) | {
        "memory_used": ["fact: likes tea", "judgement rules", "past decision: you approved 'x' (2x)"]
    }

    card = Card.model_validate(payload)

    assert [(ref.kind, ref.text) for ref in card.memory_used] == [
        (MemoryKind.FACT, "likes tea"),
        (MemoryKind.JUDGEMENT_RULES, ""),
        (MemoryKind.PAST_DECISION, "you approved 'x' (2x)"),
    ]
