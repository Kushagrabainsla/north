"""Your answer to one card can become a rule for all of north.

The memory decider asked about a fifth LinkedIn profile after you had allowed four:
an answer taught north only that exact action. After you answer a card yourself,
north now offers once to make it a rule; a rule applies in every task, flow and
session, in Safe and Autonomous, and never lets a send or a spend through.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from approval import standing_rules
from approval.approval_memory import ApprovalMemory
from approval.interaction import UserInteraction
from approval.models import ApprovalDecision, Card, CardType, DecidedBy
from approval.policy import Action, ActionKind, Answer, ApprovalPolicy, Verdict
from approval.store import ApprovalStore
from config.approval_mode import ApprovalMode
from orchestrator.orchestrator import Orchestrator

SCOPE = "browser eval on linkedin.com in profile p1 (3f2a00000000)"


def _action(**facts) -> Action:
    return Action(agent="browser", kind=ActionKind.BROWSER, summary="eval", operation="eval", rule_scope=SCOPE, **facts)


def _card(**fields) -> Card:
    return Card.new(
        type=CardType.APPROVAL,
        task_id="t1",
        agent="browser",
        title="Browser: Work",
        message="Run JavaScript on the page",
        options=["Approve", "Reject"],
        action_key="browser browser op=eval args=1",
        **({"rule_scope": SCOPE} | fields),
    )


def _orchestrator(store: ApprovalStore, memory: ApprovalMemory) -> Orchestrator:
    ledger = MagicMock()
    ledger.write = AsyncMock()
    stream = MagicMock()
    stream.emit = AsyncMock()
    return Orchestrator(
        ledger=ledger,
        agent=MagicMock(),
        north_star_checker=MagicMock(),
        failure_handler=MagicMock(),
        interaction=UserInteraction(store, stream_manager=stream),
        stream_manager=stream,
        approval_store=store,
        approval_memory=memory,
    )


class _Decider:
    def __init__(self) -> None:
        self.asked = 0

    async def rule(self, action, session=None):
        self.asked += 1
        return Answer(ApprovalDecision.APPROVED, "", "the decider said yes", DecidedBy.MEMORY_DECIDER)


async def test_your_yes_is_offered_as_a_rule_and_your_answer_keeps_it(tmp_path: Path) -> None:
    store, memory = ApprovalStore(), ApprovalMemory(tmp_path / "memory.db")
    orchestrator = _orchestrator(store, memory)
    card = _card()
    store.add(card)

    await orchestrator.respond_approval(card.id, ApprovalDecision.APPROVED, "Approve")

    [offer] = store.pending()
    assert offer.source == standing_rules.SOURCE and not offer.blocking and offer.decided_by == ""
    assert offer.options == [standing_rules.ALWAYS, standing_rules.EACH_TIME]
    assert f"allow {SCOPE}" in offer.message

    await orchestrator.respond_approval(offer.id, ApprovalDecision.ANSWERED, standing_rules.ALWAYS)

    decider = _Decider()
    policy = ApprovalPolicy(mode_provider=lambda: ApprovalMode.AUTONOMOUS, approval_memory=memory, decider=decider)
    ruling = await policy.rule(_action(), "another-task")
    assert ruling.verdict is Verdict.ALLOW and ruling.decided_by == DecidedBy.REPLAY
    assert "your standing rule" in ruling.rule and decider.asked == 0
    assert any("Standing rule: allow" in row["signature"] for row in memory.all_decisions())  # on the Memory page


async def test_asking_each_time_keeps_no_rule_and_a_kept_rule_is_not_offered_again(tmp_path: Path) -> None:
    store, memory = ApprovalStore(), ApprovalMemory(tmp_path / "memory.db")
    orchestrator = _orchestrator(store, memory)
    first = _card()
    store.add(first)
    await orchestrator.respond_approval(first.id, ApprovalDecision.APPROVED, "Approve")
    [offer] = store.pending()
    await orchestrator.respond_approval(offer.id, ApprovalDecision.ANSWERED, standing_rules.EACH_TIME)

    policy = ApprovalPolicy(mode_provider=lambda: ApprovalMode.AUTONOMOUS, approval_memory=memory, decider=_Decider())
    assert (await policy.rule(_action(), "t2")).decided_by == DecidedBy.MEMORY_DECIDER

    standing_rules.record(offer, ApprovalDecision.APPROVED, standing_rules.ALWAYS, memory)
    second = _card()
    store.add(second)
    await orchestrator.respond_approval(second.id, ApprovalDecision.APPROVED, "Approve")
    assert not store.pending()


async def test_a_standing_no_refuses_and_a_standing_yes_never_lets_a_send_through(tmp_path: Path) -> None:
    memory = ApprovalMemory(tmp_path / "memory.db")
    policy = ApprovalPolicy(mode_provider=lambda: ApprovalMode.AUTONOMOUS, approval_memory=memory, decider=_Decider())
    offer = standing_rules.offer(_card(), ApprovalDecision.APPROVED, memory)

    standing_rules.record(offer, ApprovalDecision.APPROVED, standing_rules.ALWAYS, memory)
    sending = await policy.rule(_action(reaches_third_party=True), "t1")
    assert sending.decided_by == DecidedBy.MEMORY_DECIDER  # the rule did not decide it

    standing_rules.record(offer, ApprovalDecision.REJECTED, standing_rules.NEVER, memory)
    assert (await policy.rule(_action(), "t1")).verdict is Verdict.REFUSE


def test_nothing_is_offered_for_a_card_with_no_rule_scope_or_answer_that_is_not_yes_or_no(tmp_path: Path) -> None:
    memory = ApprovalMemory(tmp_path / "memory.db")
    assert standing_rules.offer(_card(rule_scope=""), ApprovalDecision.APPROVED, memory) is None
    assert standing_rules.offer(_card(), ApprovalDecision.TASK_ENDED, memory) is None
    assert standing_rules.offer(_card(source="flow"), ApprovalDecision.APPROVED, memory) is None
    assert standing_rules.made(_card(), standing_rules.ALWAYS) is None  # not an offer
