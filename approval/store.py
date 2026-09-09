"""Registry of approval cards, persisted so a decision can wait as long as you do.

Cards are added when something needs a decision and resolved when the user
responds via the approval endpoint or Web UI. One ApprovalStore is constructed
at startup and injected everywhere - Orchestrator, AgentDependencies, the web
routes - so waits and resolutions always touch the same registry.

This used to be a plain dict, which was fine while every card lived at most a
few minutes: a card that outlived the process was a card whose asker had died
anyway. It stopped being fine once north could leave finished work for you to
review tonight. Cards are now written to SQLite and read back on startup, so
"decide whenever" survives a restart.

The `asyncio.Event` a blocking caller waits on cannot be persisted, and neither
can the coroutine behind it. So a *blocking* card found pending at startup is
resolved as `TASK_ENDED`: answering it would wake nobody, and leaving it would
ask the user to decide something that can no longer happen. Non-blocking cards
have no waiter by design and are simply loaded.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any

from approval.models import ApprovalDecision, Card
from utils.db import open_db_connection

logger = logging.getLogger(__name__)

_PENDING = "pending"

# How much *resolved* history to keep. Pending cards are never evicted - the
# whole point of the queue is that nothing you have not seen disappears from it.
_MAX_RESOLVED = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_cards (
    id         TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    status     TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT '',
    blocking   INTEGER NOT NULL DEFAULT 1,
    payload    TEXT NOT NULL
)
"""


class ApprovalStore:
    """Registry of Card objects, in memory and (optionally) on disk.

    Each card gets a paired ``asyncio.Event`` on ``add()``. Callers waiting for
    a decision use ``wait_for_decision()`` instead of polling; ``resolve()``
    sets the event so waiters wake immediately.

    Safe for concurrent coroutines on a single event loop - not thread-safe
    (``asyncio.Event`` must be set from the loop thread). All current callers
    run on the event loop.

    Without a ``db_path`` the store keeps everything in memory. That is the
    right shape for a test and for an embedded run with no home directory; it
    is not the shape the server uses.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._cards: dict[str, Card] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._db_path = db_path
        if db_path is not None:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with open_db_connection(db_path) as conn:
                conn.execute(_SCHEMA)
            self._restore(db_path)

    # ── Persistence ───────────────────────────────────────────────────────────

    def _write(self, card: Card) -> None:
        """Persist *card*, replacing any earlier version of it."""
        if self._db_path is None:
            return
        try:
            with open_db_connection(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO approval_cards (id, created_at, status, source, blocking, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET status=excluded.status, payload=excluded.payload",
                    (
                        card.id,
                        card.created_at.isoformat(),
                        card.status,
                        card.source,
                        int(card.blocking),
                        card.model_dump_json(),
                    ),
                )
        except Exception:
            logger.warning("ApprovalStore: failed to persist card %s", card.id, exc_info=True)

    def _restore(self, db_path: Path) -> None:
        """Load cards from disk, retiring the ones nobody can answer any more."""
        try:
            with open_db_connection(db_path) as conn:
                rows = conn.execute(
                    "SELECT payload FROM approval_cards ORDER BY created_at DESC LIMIT ?",
                    (_MAX_RESOLVED,),
                ).fetchall()
        except Exception:
            logger.warning("ApprovalStore: failed to read stored cards", exc_info=True)
            return

        orphaned = 0
        for row in rows:
            try:
                card = Card.model_validate(json.loads(row["payload"]))
            except Exception:
                continue
            # A blocking card's waiter died with the process. Answering it would
            # wake nobody, so it is retired rather than left asking.
            if card.status == _PENDING and card.blocking:
                card = card.model_copy(update={"status": ApprovalDecision.TASK_ENDED})
                self._write(card)
                orphaned += 1
            self._cards[card.id] = card
            if card.status == _PENDING:
                self._events[card.id] = asyncio.Event()

        waiting = sum(1 for c in self._cards.values() if c.status == _PENDING)
        if waiting or orphaned:
            logger.info(
                "ApprovalStore: restored %d card(s) still waiting on you; retired %d whose asker did not survive.",
                waiting,
                orphaned,
            )

    # ── Registry ──────────────────────────────────────────────────────────────

    def add(self, card: Card) -> None:
        self._cards[card.id] = card
        self._events[card.id] = asyncio.Event()
        self._write(card)
        self._evict_resolved()

    def _evict_resolved(self) -> None:
        """Trim resolved history past the cap. Pending cards are never touched.

        The earlier version fell back to evicting *pending* cards when resolved
        ones could not get under the cap - so a queue that filled up silently
        deleted the items nobody had looked at yet, which is the one thing it
        must never do. A queue of unanswered work is not overhead to be trimmed;
        it is the feature.
        """
        resolved = sorted(
            (c for c in self._cards.values() if c.status != _PENDING),
            key=lambda c: c.created_at,
        )
        overflow = len(resolved) - _MAX_RESOLVED
        if overflow <= 0:
            return
        evicting = resolved[:overflow]
        for card in evicting:
            self._cards.pop(card.id, None)
            self._events.pop(card.id, None)
        self._forget([card.id for card in evicting])

    def _forget(self, card_ids: list[str]) -> None:
        """Drop evicted cards from disk too, or they come back on restart."""
        if self._db_path is None or not card_ids:
            return
        ids = [(card_id,) for card_id in card_ids]
        try:
            with open_db_connection(self._db_path) as conn:
                conn.executemany("DELETE FROM approval_cards WHERE id = ?", ids)
        except Exception:
            logger.warning("ApprovalStore: failed to evict resolved cards", exc_info=True)

    def cancel_for_task(self, task_id: str) -> list[Card]:
        """Resolve the still-pending cards *task_id* was waiting on; return them.

        Called when a task reaches a terminal state. A question the task was
        blocked on stopped mattering the moment it stopped running, so leaving
        it pending would keep asking the user to decide something that can no
        longer happen. Waiters wake immediately with a `task_ended` card.

        Cards that outlive their task are skipped. For prepared work the task
        finishing is the *normal* case - north searched, drafted, and left eight
        applications for you - so sweeping them here would delete the queue at
        the exact moment it filled up.
        """
        cancelled: list[Card] = []
        for card in list(self._cards.values()):
            if card.task_id != task_id or card.status != _PENDING or card.outlives_task:
                continue
            if self.resolve(card.id, ApprovalDecision.TASK_ENDED):
                resolved = self._cards.get(card.id)
                if resolved is not None:
                    cancelled.append(resolved)
        if cancelled:
            logger.info("Cancelled %d pending approval card(s) for ended task %s", len(cancelled), task_id)
        return cancelled

    def resolve(
        self,
        card_id: str,
        status: str,
        chosen_option: str = "",
        values: dict[str, Any] | None = None,
    ) -> bool:
        """Resolve a pending card and wake any waiting coroutines.

        Returns True when the card existed and was pending. A card that is
        unknown or already resolved is left untouched (False) - a decision
        binds to exactly one issued card and cannot be replayed or overwritten.

        *values* are the field values the user decided on. They are merged
        against the issued card rather than stored as sent, so a card that
        carries work always resolves to a complete, server-validated set - and
        a card resolved without any (a timeout, a learned rule) still reports
        what north had proposed.
        """
        card = self._cards.get(card_id)
        if card is None or card.status != _PENDING:
            return False
        update: dict[str, Any] = {"status": status, "chosen_option": chosen_option}
        if card.fields:
            update["response"] = card.merge_response(values)
        resolved = card.model_copy(update=update)
        self._cards[card_id] = resolved
        self._write(resolved)
        event = self._events.get(card_id)
        if event is not None:
            event.set()
        # Resolving is what makes a card history, so it is also when history can
        # go over its cap. Trimming only on `add` left the count one past the
        # limit until the next card happened to arrive.
        self._evict_resolved()
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

    def waiting_for_you(self) -> list[Card]:
        """Pending cards nothing is blocked on - prepared work, oldest first."""
        return sorted(
            (c for c in self._cards.values() if c.status == _PENDING and not c.blocking),
            key=lambda c: c.created_at,
        )

    def all(self, limit: int = 100) -> list[Card]:
        cards = list(self._cards.values())
        cards.sort(key=lambda c: c.created_at, reverse=True)
        return cards[:limit]
