"""Surface a decision/question card to the user and block until it resolves.

`request_approval` (an APPROVAL card) and `ask_user` (a QUESTION card) are the
same interaction with different framing: optionally short-circuit on a learned
judgement rule, otherwise add the card to the store, emit its SSE event, and
await the user's resolution. This module owns that one mechanism so the two
agent tools stay DRY and consistent.

See docs/CODING_STYLE.md Section 15.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from approval.models import ApprovalDecision, Card, CardType
from utils.ids import generate_id

if TYPE_CHECKING:
    from approval.judgement_filter import JudgementFilter
    from approval.store import ApprovalStore

logger = logging.getLogger(__name__)


class CardEvent(StrEnum):
    """SSE event a card interaction emits to connected clients."""

    APPROVAL = "approval_required"
    QUESTION = "question_required"


# Per-event key the card body is sent under - clients read these exact keys.
_EVENT_BODY_KEY: dict[CardEvent, str] = {
    CardEvent.APPROVAL: "message",
    CardEvent.QUESTION: "question",
}

# Default choices for an approval with no caller-supplied options.
APPROVAL_DEFAULT_OPTIONS: tuple[str, ...] = ("Approve", "Reject")


async def _auto_resolve(
    card: Card, judgement_filter: JudgementFilter | None, agent_name: str
) -> Card | None:
    """Return a card pre-resolved by a learned rule, or ``None`` to surface it.

    Failures are swallowed: a judgement-filter error must never block the user
    from being asked, so we fall through to surfacing the card.
    """
    if judgement_filter is None:
        return None
    try:
        decision, chosen_option = await judgement_filter.check(card)
    except Exception:
        logger.debug("JudgementFilter check failed for agent %s - surfacing card", agent_name)
        return None
    if decision is None:
        return None
    logger.info("JudgementFilter auto-%s for agent %s: %r", decision, agent_name, card.message[:80])
    return card.model_copy(update={"status": decision, "chosen_option": chosen_option})


async def surface_card(
    *,
    store: ApprovalStore,
    stream_manager: Any | None,
    judgement_filter: JudgementFilter | None,
    timeout: float,
    agent_name: str,
    task_id: str,
    card_type: CardType,
    title: str,
    body: str,
    options: list[str],
    event: CardEvent,
) -> Card:
    """Resolve a card: a learned-rule auto-decision if one fires, else surface it
    to the user and block until they respond.

    Always returns a *resolved* ``Card`` (its ``status``/``chosen_option`` set), so
    callers read the outcome uniformly. On timeout the card is marked
    ``TIMEOUT_REJECTED`` rather than left pending.
    """
    card = Card(
        id=generate_id(),
        type=card_type,
        task_id=task_id,
        agent=agent_name,
        title=title,
        message=body,
        options=options,
    )

    auto = await _auto_resolve(card, judgement_filter, agent_name)
    if auto is not None:
        return auto

    store.add(card)
    if stream_manager is not None and task_id:
        await stream_manager.emit(
            task_id,
            event.value,
            {
                "card_id": card.id,
                "task_id": task_id,
                "agent": agent_name,
                "title": title,
                _EVENT_BODY_KEY[event]: body,
                "options": options,
            },
        )

    resolved = await store.wait_for_decision(card.id, timeout=timeout)
    if resolved is None:
        store.resolve(card.id, ApprovalDecision.TIMEOUT_REJECTED)
        return card.model_copy(update={"status": ApprovalDecision.TIMEOUT_REJECTED})
    return resolved
