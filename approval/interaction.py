"""Single mediator for every user-facing card interaction.

`UserInteraction` is the one place north turns a `Card` into a user prompt: it
applies the learned `JudgementFilter`, registers the card in the `ApprovalStore`,
surfaces it (SSE stream + TUI-aware `Notifier`), and - for decisions - blocks
until the user responds or the timeout elapses.

Tools, agents, and the Orchestrator all go through this class, so the
surface -> await -> resolve sequence exists exactly once (DRY / SRP). Each caller
supplies only the dependencies it has: the Orchestrator wires a `Notifier` and an
auto-resolve audit hook; tools and agents pass the stream manager. A missing
dependency simply means that channel is skipped.

See docs/CODING_STYLE.md Section 15.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from approval.models import ApprovalDecision, Card, CardType
from utils.ids import generate_id

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from approval.base import Notifier
    from approval.judgement_filter import JudgementFilter
    from approval.store import ApprovalStore

logger = logging.getLogger(__name__)

# Default choices for an approval card when the caller supplies none.
APPROVAL_DEFAULT_OPTIONS: tuple[str, str] = ("Approve", "Reject")

_PENDING = "pending"


class CardEvent(StrEnum):
    """SSE event name a card interaction emits to connected clients."""

    APPROVAL = "approval_required"
    QUESTION = "question_required"


# Per-event key the card body is sent under - clients read these exact keys.
_EVENT_BODY_KEY: dict[CardEvent, str] = {
    CardEvent.APPROVAL: "message",
    CardEvent.QUESTION: "question",
}


class UserInteraction:
    """The single entry point for surfacing cards and awaiting user decisions.

    One instance is wired per caller with whatever dependencies it has. The
    Orchestrator passes a `Notifier` (system alert) and an ``on_auto_resolve``
    audit hook; tools and agents pass a stream manager for SSE. Behaviour is the
    superset that each channel allows - absent dependencies are skipped, never
    reimplemented elsewhere.
    """

    def __init__(
        self,
        store: ApprovalStore,
        *,
        notifier: Notifier | None = None,
        judgement_filter: JudgementFilter | None = None,
        stream_manager: Any | None = None,
        on_auto_resolve: Callable[[Card, str, str], Awaitable[None]] | None = None,
        default_timeout: float = 300.0,
    ) -> None:
        self._store = store
        self._notifier = notifier
        self._judgement_filter = judgement_filter
        self._stream = stream_manager
        self._on_auto_resolve = on_auto_resolve
        self._default_timeout = default_timeout

    async def request_approval(
        self,
        *,
        task_id: str | None,
        agent: str,
        title: str,
        message: str,
        options: tuple[str, ...] | list[str] = APPROVAL_DEFAULT_OPTIONS,
        timeout: float | None = None,
    ) -> bool:
        """Surface an APPROVAL card and block; return True only if approved."""
        card = self._build(CardType.APPROVAL, task_id, agent, title, message, list(options))
        resolved = await self.request_decision(card, event=CardEvent.APPROVAL, timeout=timeout)
        return resolved.status == ApprovalDecision.APPROVED

    async def ask_user(
        self,
        *,
        task_id: str | None,
        agent: str,
        title: str,
        question: str,
        options: list[str],
        timeout: float | None = None,
    ) -> Card:
        """Surface a QUESTION card and block; return the resolved card.

        The user's answer (a chosen option or free text) is on ``chosen_option``.
        """
        card = self._build(CardType.QUESTION, task_id, agent, title, question, list(options))
        return await self.request_decision(card, event=CardEvent.QUESTION, timeout=timeout)

    async def inform(self, *, task_id: str | None, agent: str, title: str, message: str) -> None:
        """Surface an INFORMATION card. Never blocks."""
        card = self._build(CardType.INFORMATION, task_id, agent, title, message, [])
        await self.notify(card)

    async def request_decision(
        self, card: Card, *, event: CardEvent | None = None, timeout: float | None = None
    ) -> Card:
        """Surface *card* and block until it resolves; return the resolved card.

        A learned rule may resolve it immediately. A timeout resolves it as
        ``TIMEOUT_REJECTED`` - unless the user resolved it at the same instant,
        in which case their decision is honoured.
        """
        surfaced = await self.notify(card, event=event)
        if surfaced.status != _PENDING:
            return surfaced  # auto-resolved by a learned rule

        resolved = await self._store.wait_for_decision(card.id, timeout=timeout or self._default_timeout)
        if resolved is not None:
            return resolved
        if self._store.resolve(card.id, ApprovalDecision.TIMEOUT_REJECTED):
            return card.model_copy(update={"status": ApprovalDecision.TIMEOUT_REJECTED})
        late = self._store.get(card.id)
        return late if late is not None else card.model_copy(update={"status": ApprovalDecision.TIMEOUT_REJECTED})

    async def notify(self, card: Card, *, event: CardEvent | None = None) -> Card:
        """Register and surface *card* without blocking; return it.

        The returned card is already resolved if a learned rule fired. Surfacing
        skips whichever channel is absent: SSE only when an *event* and a stream
        manager are present; a system alert only when a Notifier is wired.
        """
        auto = await self._auto_resolve(card)
        if auto is not None:
            return auto
        self._store.add(card)
        if event is not None:
            await self._emit(card, event)
        if self._notifier is not None:
            await self._notifier.notify(card)
        return card

    async def _auto_resolve(self, card: Card) -> Card | None:
        """Resolve *card* from a learned rule, or return None to surface it.

        A JudgementFilter error never blocks the user from being asked - we log
        and fall through to surfacing the card.
        """
        if self._judgement_filter is None:
            return None
        try:
            decision, chosen_option = await self._judgement_filter.check(card)
        except Exception:
            logger.debug("JudgementFilter check failed for agent %s - surfacing card", card.agent)
            return None
        if decision is None:
            return None
        self._store.add(card)
        self._store.resolve(card.id, decision, chosen_option=chosen_option or "")
        if self._on_auto_resolve is not None:
            await self._on_auto_resolve(card, decision, chosen_option or "")
        logger.info("Auto-%s for agent %s via learned rule", decision, card.agent)
        return card.model_copy(update={"status": decision, "chosen_option": chosen_option or ""})

    async def _emit(self, card: Card, event: CardEvent) -> None:
        if self._stream is None or not card.task_id:
            return
        await self._stream.emit(
            card.task_id,
            event.value,
            {
                "card_id": card.id,
                "task_id": card.task_id,
                "agent": card.agent,
                "title": card.title,
                _EVENT_BODY_KEY[event]: card.message,
                "options": card.options,
            },
        )

    @staticmethod
    def _build(
        card_type: CardType, task_id: str | None, agent: str, title: str, body: str, options: list[str]
    ) -> Card:
        return Card(
            id=generate_id(),
            type=card_type,
            task_id=task_id or "",
            agent=agent,
            title=title,
            message=body,
            options=options,
        )
