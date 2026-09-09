"""Composite notifier that suppresses duplicate alerts when the TUI is watching.

When the TUI is open it receives approval_required events via the global SSE
stream and handles them inline, so firing a second notification on top would be
redundant. That reasoning holds for a card raised by the task you are watching
and fails for one raised at 03:00 by cron while a TUI happens to be left open:
nothing on screen is about that task, and the SSE event scrolls past unread.

So the question is not "is a TUI connected" but "was this card raised by
something the user is looking at".
"""

from __future__ import annotations

from approval.base import Notifier
from approval.models import Card


class TUIAwareNotifier(Notifier):
    """Delegates to fallback unless the TUI is already showing this card."""

    def __init__(self, stream_manager: object, fallback: Notifier) -> None:
        # stream_manager is EventStreamManager - typed as object to avoid
        # circular imports at module level.
        self._sm = stream_manager
        self._fallback = fallback

    async def notify(self, card: Card) -> None:
        if getattr(self._sm, "tui_connected", False) and not card.outlives_task:
            # The global SSE stream already carries the approval_required event
            # emitted by the orchestrator - the TUI will handle it inline.
            return
        # `outlives_task` means the card was not raised by an agent waiting in
        # front of the user: it is prepared work, or it came from a task with a
        # `source` such as a schedule. Either way an open TUI is not evidence
        # anyone saw it, so it goes out through the real notifier.
        await self._fallback.notify(card)
