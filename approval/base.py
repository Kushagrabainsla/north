"""Abstract base class for the Notifier interface.

See docs/CODING_STYLE.md Section 6.1 and Section 16.1.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from approval.models import Card


class Notifier(ABC):
    """Abstract interface for sending alerts to the user.

    Implementations: MacOSNotifier, TerminalNotifier.
    """

    @abstractmethod
    async def notify(self, card: Card) -> None:
        """Deliver a card to the user as an alert or notification.

        Args:
            card: The Card instance containing the details to present.

        Raises:
            NotificationError: If the notification delivery fails.
        """
