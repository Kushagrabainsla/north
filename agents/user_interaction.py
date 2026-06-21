"""Agent-facing helper for surfacing approval/question cards to the user.

``request_approval`` (an APPROVAL card) and ``ask_user`` (a QUESTION card) are
the same interaction with different framing. Both delegate to the shared
``approval.interaction.UserInteraction`` mediator so the surface -> await ->
resolve sequence lives in exactly one place (DRY).

See docs/CODING_STYLE.md Section 15.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from approval.interaction import APPROVAL_DEFAULT_OPTIONS, CardEvent, UserInteraction
from approval.models import Card, CardType
from utils.ids import generate_id

if TYPE_CHECKING:
    from approval.base import Notifier
    from approval.judgement_filter import JudgementFilter
    from approval.store import ApprovalStore

__all__ = ["APPROVAL_DEFAULT_OPTIONS", "CardEvent", "surface_card"]


async def surface_card(
    *,
    store: ApprovalStore,
    stream_manager: Any | None,
    judgement_filter: JudgementFilter | None,
    notifier: Notifier | None = None,
    timeout: float,
    agent_name: str,
    task_id: str,
    card_type: CardType,
    title: str,
    body: str,
    options: list[str],
    event: CardEvent,
) -> Card:
    """Surface a card and block until it resolves; return it resolved.

    Thin agent-facing adapter over ``UserInteraction`` (see module docstring).
    Always returns a resolved card - a learned rule may answer it, and a timeout
    resolves it as ``TIMEOUT_REJECTED``.
    """
    interaction = UserInteraction(
        store,
        notifier=notifier,
        judgement_filter=judgement_filter,
        stream_manager=stream_manager,
        default_timeout=timeout,
    )
    card = Card(
        id=generate_id(),
        type=card_type,
        task_id=task_id,
        agent=agent_name,
        title=title,
        message=body,
        options=options,
    )
    return await interaction.request_decision(card, event=event, timeout=timeout)
