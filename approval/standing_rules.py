"""Standing rules: your answer to one card, made a rule for all of north.

In Autonomous mode the memory decider leaves a card for you when nothing you stated
covers a step, and your answer used to teach north only that exact action: on
2026-11-18 it asked about a fifth LinkedIn profile after you had allowed four. Now,
after you answer a card yourself, north offers once to make the answer a rule. The
rule names the kind of action and where, in the words of the tool (`Action.rule_scope`),
and applies in every task, flow and session, in the modes that replay your answers
(Safe and Autonomous). A rule is one row of approval memory, so the Memory page lists
it and can remove it.

Only a yes that may stand for the next one is offered: never a send, a spend or a
change to north's own settings (`approval.policy.may_replay_yes`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from approval.models import ApprovalDecision, Card, CardType
from approval.policy import standing_rule_key

if TYPE_CHECKING:
    from approval.approval_memory import ApprovalMemory

SOURCE = "standing_rule"
ALWAYS = "Yes, always allow it"
NEVER = "Yes, never allow it"
EACH_TIME = "No, ask me each time"
_CARD_TEXT_CHARS = 300


def offer(card: Card, decision: str, memory: ApprovalMemory) -> Card | None:
    """The card that asks whether to make your answer to *card* a rule, or None when none fits.

    None for a card that names no rule scope, one north answered, an answer that is not a yes
    or a no, and a scope that already has a rule.
    """
    if card.type is not CardType.APPROVAL or not card.rule_scope or card.source:
        return None
    if decision not in (ApprovalDecision.APPROVED, ApprovalDecision.REJECTED):
        return None
    if memory.recall(card.agent, standing_rule_key(card.rule_scope)) is not None:
        return None
    allowed = decision == ApprovalDecision.APPROVED
    verb = "allow" if allowed else "refuse"
    answered = card.message.strip()[:_CARD_TEXT_CHARS]
    return Card.new(
        type=CardType.QUESTION,
        task_id=card.task_id,
        agent=card.agent,
        title="Make this a rule?",
        message=(
            f"You {'approved' if allowed else 'rejected'} this:\n{answered}\n\n"
            f"Should north {verb} it from now on, in every task, flow and session, without asking?\n"
            f"The rule: {verb} {card.rule_scope}.\n"
            "It applies in Safe and Autonomous modes, and you can remove it on the Memory page."
        ),
        options=[ALWAYS if allowed else NEVER, EACH_TIME],
        rejecting=[EACH_TIME],
        blocking=False,
        source=SOURCE,
        rule_scope=card.rule_scope,
    )


def made(card: Card, chosen_option: str) -> str | None:
    """The decision ("approved" or "rejected") your answer to an offer makes a rule, or None."""
    if card.source != SOURCE or not card.rule_scope:
        return None
    return {ALWAYS: ApprovalDecision.APPROVED, NEVER: ApprovalDecision.REJECTED}.get(chosen_option)


def record(card: Card, decision: str, chosen_option: str, memory: ApprovalMemory) -> None:
    """Keep the rule your answer made, under the key the policy recalls it by."""
    verb = "allow" if decision == ApprovalDecision.APPROVED else "refuse"
    labelled = card.model_copy(update={"action_label": f"Standing rule: {verb} {card.rule_scope}"})
    memory.record(card.agent, standing_rule_key(card.rule_scope), decision, card=labelled, chosen_option=chosen_option)
