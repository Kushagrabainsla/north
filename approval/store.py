"""In-memory store for pending approval cards shown in the Web UI.

Cards are added when a Notifier fires and resolved when the user responds
via the approval endpoint or Web UI. The store is a module-level singleton
so it is accessible from both the Notifier implementations and the web routes
without requiring constructor injection across the whole call chain.
"""

from __future__ import annotations

from approval.models import Card


class ApprovalStore:
    """Thread-safe in-memory registry of Card objects."""

    def __init__(self) -> None:
        self._cards: dict[str, Card] = {}

    def add(self, card: Card) -> None:
        self._cards[card.id] = card

    def resolve(self, card_id: str, status: str) -> None:
        """Update the status of a card (approved / rejected / answered)."""
        if card_id in self._cards:
            self._cards[card_id] = self._cards[card_id].model_copy(
                update={"status": status}
            )

    def get(self, card_id: str) -> Card | None:
        return self._cards.get(card_id)

    def pending(self) -> list[Card]:
        return [c for c in self._cards.values() if c.status == "pending"]

    def all(self, limit: int = 100) -> list[Card]:
        cards = list(self._cards.values())
        cards.sort(key=lambda c: c.created_at, reverse=True)
        return cards[:limit]


# Module-level singleton — import this directly everywhere.
approval_store = ApprovalStore()
