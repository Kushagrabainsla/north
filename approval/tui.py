"""Composite notifier that suppresses system notifications when the TUI is connected.

When the TUI is open it receives approval_required events via the global SSE
stream and handles them inline.  Firing a macOS notification on top of that
would be redundant and confusing.  This notifier checks tui_connected and
either no-ops (TUI handles it) or delegates to the real notifier (no TUI).
"""

from __future__ import annotations

from approval.base import Notifier
from approval.models import Card


class TUIAwareNotifier(Notifier):
    """Delegates to fallback only when no TUI session is active."""

    def __init__(self, stream_manager: object, fallback: Notifier) -> None:
        # stream_manager is EventStreamManager — typed as object to avoid
        # circular imports at module level.
        self._sm = stream_manager
        self._fallback = fallback

    async def notify(self, card: Card) -> None:
        if getattr(self._sm, "tui_connected", False):
            # The global SSE stream already carries the approval_required event
            # emitted by the orchestrator — the TUI will handle it inline.
            return
        await self._fallback.notify(card)
