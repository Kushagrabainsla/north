"""In-memory store for pending approval cards shown in the Web UI.

Cards are added when a Notifier fires and resolved when the user responds
via the approval endpoint or Web UI. A single ApprovalStore instance is
constructed at startup and injected wherever it is needed — Orchestrator,
AgentDependencies, and the web routes all share the same object so that
approval waits and resolutions always touch the same in-memory registry.
"""

from __future__ import annotations

import asyncio

from approval.models import Card


class ApprovalStore:
    """Thread-safe in-memory registry of Card objects.

    Each card gets a paired ``asyncio.Event`` on ``add()``.  Callers waiting
    for a decision use ``wait_for_decision()`` instead of polling; ``resolve()``
    sets the event so waiters wake immediately.
    """

    def __init__(self) -> None:
        self._cards: dict[str, Card] = {}
        self._events: dict[str, asyncio.Event] = {}

    def add(self, card: Card) -> None:
        self._cards[card.id] = card
        self._events[card.id] = asyncio.Event()

    def resolve(self, card_id: str, status: str, chosen_option: str = "") -> None:
        """Update the status of a card and wake any waiting coroutines."""
        if card_id in self._cards:
            self._cards[card_id] = self._cards[card_id].model_copy(
                update={"status": status, "chosen_option": chosen_option}
            )
            event = self._events.get(card_id)
            if event is not None:
                event.set()

    async def wait_for_decision(self, card_id: str, timeout: float = 300.0) -> Card | None:
        """Block until the card is resolved or *timeout* seconds elapse.

        Returns the resolved ``Card`` (status ≠ "pending") or ``None`` on
        timeout.  Never polls; wakes exactly when ``resolve()`` is called.
        The asyncio.Event is freed after this method returns so it does not
        accumulate indefinitely in long-running servers.
        """
        event = self._events.get(card_id)
        if event is None:
            return self._cards.get(card_id)
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            pass
        finally:
            # Release the Event regardless of outcome — it is no longer needed
            # once we have woken up (either resolved or timed out).
            self._events.pop(card_id, None)
        card = self._cards.get(card_id)
        if card is None or card.status == "pending":
            return None
        return card

    def get(self, card_id: str) -> Card | None:
        return self._cards.get(card_id)

    def pending(self) -> list[Card]:
        return [c for c in self._cards.values() if c.status == "pending"]

    def all(self, limit: int = 100) -> list[Card]:
        cards = list(self._cards.values())
        cards.sort(key=lambda c: c.created_at, reverse=True)
        return cards[:limit]
