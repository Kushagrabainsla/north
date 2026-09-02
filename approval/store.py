"""In-memory store for pending approval cards shown in the Web UI.

Cards are added when a Notifier fires and resolved when the user responds
via the approval endpoint or Web UI. A single ApprovalStore instance is
constructed at startup and injected wherever it is needed - Orchestrator,
AgentDependencies, and the web routes all share the same object so that
approval waits and resolutions always touch the same in-memory registry.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from approval.models import ApprovalDecision, Card

logger = logging.getLogger(__name__)

_PENDING = "pending"

# Bound the in-memory registry so a long-running server does not accumulate cards
# forever. Resolved cards are evicted oldest-first; pending ones are kept, which
# is only safe because `cancel_for_task` resolves a task's cards when it ends.
_MAX_CARDS = 500


class ApprovalStore:
    """Coroutine-safe in-memory registry of Card objects.

    Each card gets a paired ``asyncio.Event`` on ``add()``.  Callers waiting
    for a decision use ``wait_for_decision()`` instead of polling; ``resolve()``
    sets the event so waiters wake immediately.

    Safe for concurrent coroutines on a single event loop - not thread-safe
    (``asyncio.Event`` must be set from the loop thread). All current callers
    run on the event loop.
    """

    def __init__(self) -> None:
        self._cards: dict[str, Card] = {}
        self._events: dict[str, asyncio.Event] = {}

    def add(self, card: Card) -> None:
        self._cards[card.id] = card
        self._events[card.id] = asyncio.Event()
        self._evict_resolved()

    def _evict_resolved(self) -> None:
        """Drop the oldest resolved cards once the registry exceeds its cap.

        Falls back to evicting the oldest cards of any status when resolved ones
        alone cannot get under the cap. Without that fallback the cap was not a
        cap at all: pending cards are never evicted, so any that outlive their
        task accumulate without bound. `cancel_for_task` is what normally keeps
        that from happening; this is the backstop if a path ever misses it.
        """
        overflow = len(self._cards) - _MAX_CARDS
        if overflow <= 0:
            return
        by_age = sorted(self._cards.values(), key=lambda c: c.created_at)
        resolved = [c for c in by_age if c.status != _PENDING]
        evicting = resolved[:overflow]
        if len(evicting) < overflow:
            still_over = overflow - len(evicting)
            already = {c.id for c in evicting}
            stale_pending = [c for c in by_age if c.id not in already][:still_over]
            logger.warning(
                "ApprovalStore over cap with %d pending card(s) - evicting the %d oldest. "
                "A task likely ended without its cards being cancelled.",
                sum(1 for c in self._cards.values() if c.status == _PENDING),
                len(stale_pending),
            )
            evicting += stale_pending
        for card in evicting:
            self._cards.pop(card.id, None)
            self._events.pop(card.id, None)

    def cancel_for_task(self, task_id: str) -> list[Card]:
        """Resolve every still-pending card for *task_id*; return those resolved.

        Called when a task reaches a terminal state. Its questions stopped
        mattering the moment it stopped running, so leaving them pending would
        keep asking the user to decide something that can no longer happen - and
        would hold the card and its event forever, since pending cards are not
        evicted. Waiters wake immediately with a `task_ended` card.
        """
        cancelled: list[Card] = []
        for card in list(self._cards.values()):
            if card.task_id != task_id or card.status != _PENDING:
                continue
            if self.resolve(card.id, ApprovalDecision.TASK_ENDED):
                resolved = self._cards.get(card.id)
                if resolved is not None:
                    cancelled.append(resolved)
        if cancelled:
            logger.info("Cancelled %d pending approval card(s) for ended task %s", len(cancelled), task_id)
        return cancelled

    def resolve(self, card_id: str, status: str, chosen_option: str = "") -> bool:
        """Resolve a pending card and wake any waiting coroutines.

        Returns True when the card existed and was pending. A card that is
        unknown or already resolved is left untouched (False) - a decision
        binds to exactly one issued card and cannot be replayed or overwritten.
        """
        card = self._cards.get(card_id)
        if card is None or card.status != _PENDING:
            return False
        self._cards[card_id] = card.model_copy(update={"status": status, "chosen_option": chosen_option})
        event = self._events.get(card_id)
        if event is not None:
            event.set()
        return True

    async def wait_for_decision(self, card_id: str, timeout: float = 300.0) -> Card | None:
        """Block until the card is resolved or *timeout* seconds elapse.

        Returns the resolved ``Card`` (status ≠ "pending") or ``None`` on
        timeout. Never polls; wakes exactly when ``resolve()`` is called.
        """
        event = self._events.get(card_id)
        if event is None:
            card = self._cards.get(card_id)
            return card if (card and card.status != _PENDING) else None
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), timeout=timeout)
        finally:
            self._events.pop(card_id, None)

        card = self._cards.get(card_id)
        if card is None or card.status == _PENDING:
            return None
        return card

    def get(self, card_id: str) -> Card | None:
        return self._cards.get(card_id)

    def pending(self) -> list[Card]:
        return [c for c in self._cards.values() if c.status == _PENDING]

    def all(self, limit: int = 100) -> list[Card]:
        cards = list(self._cards.values())
        cards.sort(key=lambda c: c.created_at, reverse=True)
        return cards[:limit]
