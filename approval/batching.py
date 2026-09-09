"""One notification for a batch of prepared work, instead of one per item.

A guard-rail card is urgent by construction - an agent is mid-action and cannot
continue - so it is delivered the moment it arrives. Prepared work is not: north
finishing eight job applications is one event in the user's day, not eight, and
alerting per card turns the feature that was meant to save attention into the
thing costing it.

So non-blocking cards are collected for a short window and delivered as a single
"north has N things for you". The window is deliberately short - long enough to
coalesce one batch north produced in a burst, not long enough that something
sitting in the queue goes unmentioned.
"""

from __future__ import annotations

import asyncio
import logging

from approval.base import Notifier
from approval.models import Card, CardType

logger = logging.getLogger(__name__)

# How long to wait for more prepared work before announcing what has arrived.
DEFAULT_WINDOW_SECONDS = 20.0


class BatchingNotifier(Notifier):
    """Delivers urgent cards at once and coalesces prepared work into one alert."""

    def __init__(self, fallback: Notifier, window_seconds: float = DEFAULT_WINDOW_SECONDS) -> None:
        self._fallback = fallback
        self._window = window_seconds
        self._batch: list[Card] = []
        self._flush_task: asyncio.Task | None = None

    async def notify(self, card: Card) -> None:
        if card.blocking or card.type is not CardType.APPROVAL:
            await self._fallback.notify(card)
            return
        self._batch.append(card)
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_after_window())

    async def _flush_after_window(self) -> None:
        try:
            await asyncio.sleep(self._window)
            await self.flush()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("BatchingNotifier: failed to deliver a batch", exc_info=True)

    async def flush(self) -> None:
        """Deliver whatever has accumulated, as one card. Safe to call empty."""
        batch, self._batch = self._batch, []
        if not batch:
            return
        if len(batch) == 1:
            await self._fallback.notify(batch[0])
            return
        agents = sorted({card.agent for card in batch})
        await self._fallback.notify(
            Card.new(
                type=CardType.INFORMATION,
                task_id=batch[-1].task_id,
                agent=", ".join(agents),
                title=f"{len(batch)} things waiting for you",
                message="north has finished these and left them for your decision:\n"
                + "\n".join(f"  • {card.title}" for card in batch[:10])
                + (f"\n  … and {len(batch) - 10} more" if len(batch) > 10 else ""),
            )
        )
