"""Standing rules: your answer to one card, made a rule for all of north.

In Autonomous mode the memory decider leaves a card for you when nothing you stated
covers a step, and your answer used to teach north only that exact action: on
2026-11-18 it asked about a fifth LinkedIn profile after you had allowed four. Two
cards now turn answers into rules:

- After you answer a card yourself, north offers once to make your answer a rule.
- Each morning (the built-in rules-to-confirm flow), one card in your attention panel
  lists the kinds of action the memory decider ruled on at least twice in the last day
  and that have no rule, and asks for each whether its ruling should become yours. A decider's yes is not your yes: only
  your answer makes the rule.

A rule names the kind of action and where, in the words of the tool (`Action.rule_scope`),
and applies in every task, flow and session, in the modes that replay your answers (Safe
and Autonomous). It is one row of approval memory, so the Memory page lists it and can
remove it. "No, ask me each time" is kept too, so north does not ask about that kind again.

Only a yes that may stand for the next one is offered: never a send, a spend or a change
to north's own settings (`approval.policy.may_replay_yes`).
"""

from __future__ import annotations

import datetime
import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from approval.models import ApprovalDecision, Card, CardField, CardType, DecidedBy
from approval.policy import standing_rule_key
from utils.time import local_timezone

if TYPE_CHECKING:
    from approval.approval_memory import ApprovalMemory
    from approval.interaction import UserInteraction
    from approval.store import ApprovalStore

logger = logging.getLogger(__name__)

SOURCE = "standing_rule"
DIGEST_SOURCE = "standing_rule_digest"
ALWAYS = "Yes, always allow it"
NEVER = "Yes, never allow it"
EACH_TIME = "No, ask me each time"
# What "ask me each time" is kept as: a decision that never replays, and settles the scope.
DECLINED = "declined"
# The morning card (the built-in rules-to-confirm flow, 08:00 by default): a kind of action ruled on at
# least this often.
_MIN_RULINGS = 2
_MAX_DIGEST_QUESTIONS = 5
_CARD_TEXT_CHARS = 300
_ANSWERS = {ALWAYS: ApprovalDecision.APPROVED, NEVER: ApprovalDecision.REJECTED, EACH_TIME: DECLINED}


def settled(agent: str, scope: str, memory: ApprovalMemory) -> bool:
    """Whether you already answered for *scope*: a rule, or "ask me each time"."""
    return memory.latest(agent, standing_rule_key(scope)) is not None


def offer(card: Card, decision: str, memory: ApprovalMemory) -> Card | None:
    """The card that asks whether to make your answer to *card* a rule, or None when none fits.

    None for a card that names no rule scope, one north made for itself, an answer that is
    not a yes or a no, and a scope you already answered for.
    """
    if card.type is not CardType.APPROVAL or not card.rule_scope or card.source:
        return None
    if decision not in (ApprovalDecision.APPROVED, ApprovalDecision.REJECTED):
        return None
    if settled(card.agent, card.rule_scope, memory):
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


def digest(decided: list[Card], memory: ApprovalMemory) -> Card | None:
    """The morning card: what the memory decider ruled on at least twice, unsettled, as one question each.

    A kind of action it both allowed and refused is left out: its rulings do not say what a rule
    should be. The kinds ruled on most come first.
    """
    rulings: dict[tuple[str, str], list[str]] = defaultdict(list)
    for card in decided:
        if card.decided_by == DecidedBy.MEMORY_DECIDER and card.rule_scope and card.status in _RULINGS:
            rulings[(card.agent, card.rule_scope)].append(card.status)
    fields = []
    for (agent, scope), statuses in sorted(rulings.items(), key=lambda item: -len(item[1])):
        if len(statuses) < _MIN_RULINGS or len(set(statuses)) > 1 or settled(agent, scope, memory):
            continue
        allowed = statuses[0] == ApprovalDecision.APPROVED
        verb = "allow" if allowed else "refuse"
        fields.append(
            CardField(
                name=f"{agent}|{scope}",
                label=(
                    f"North's approval model {'allowed' if allowed else 'refused'} this {len(statuses)} times: "
                    f"{verb} {scope}. Make it your rule?"
                ),
                editable=True,
                options=[ALWAYS if allowed else NEVER, EACH_TIME],
            )
        )
        if len(fields) == _MAX_DIGEST_QUESTIONS:
            break
    if not fields:
        return None
    return Card.new(
        type=CardType.QUESTION,
        task_id="",
        agent="north",
        title="Rules to confirm",
        message=(
            "North's approval model decided these for you more than once yesterday. Each answer of yours "
            "becomes a rule for every task, flow and session in Safe and Autonomous modes, or stops north "
            "asking about it. You can remove a rule on the Memory page."
        ),
        fields=fields,
        blocking=False,
        source=DIGEST_SOURCE,
    )


_RULINGS = (ApprovalDecision.APPROVED, ApprovalDecision.REJECTED)


def answers(card: Card, chosen_option: str, values: dict[str, str]) -> list[tuple[str, str, str]]:
    """What your answer to an offer or a morning card settles: (agent, scope, decision) for each question."""
    if card.source == SOURCE and card.rule_scope:
        asked = [(card.agent, card.rule_scope, chosen_option)]
    elif card.source == DIGEST_SOURCE:
        asked = [(*field.name.split("|", 1), values.get(field.name, "")) for field in card.fields if "|" in field.name]
    else:
        return []
    return [(agent, scope, decision) for agent, scope, answer in asked if (decision := _ANSWERS.get(str(answer)))]


def keep(agent: str, scope: str, decision: str, memory: ApprovalMemory, card: Card) -> None:
    """Keep what you settled for *scope*, under the key the policy recalls it by."""
    if decision == DECLINED:
        label = f"No rule, ask each time: {scope}"
    else:
        label = f"Standing rule: {'allow' if decision == ApprovalDecision.APPROVED else 'refuse'} {scope}"
    labelled = card.model_copy(update={"action_label": label})
    memory.record(agent, standing_rule_key(scope), decision, card=labelled)


async def offer_morning_digest(
    store: ApprovalStore, memory: ApprovalMemory, interaction: UserInteraction, now: datetime.datetime
) -> Card | None:
    """Today's card, put in your attention panel, or None when today already has one or nothing is unsettled.

    The step of the built-in rules-to-confirm flow, whose schedule says when; a run caught up after the Mac
    slept makes no second card the same day.
    """
    from approval.interaction import CardEvent

    today = now.date()
    zone = local_timezone()
    if any(card.source == DIGEST_SOURCE and card.created_at.astimezone(zone).date() == today for card in store.all()):
        return None
    card = digest(store.decided_since(now - datetime.timedelta(days=1)), memory)
    if card is not None:
        await interaction.offer(card, event=CardEvent.QUESTION)
    return card
