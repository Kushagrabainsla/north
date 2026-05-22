"""Terminal-backed implementation of the Notifier interface.

Useful in development and testing. Prints formatted cards to the terminal.
"""

from __future__ import annotations

import sys

from approval.base import Notifier
from approval.models import Card, CardType


class TerminalNotifier(Notifier):
    """Prints Card details directly to standard output."""

    async def notify(self, card: Card) -> None:
        """Draw a box around the card details and print it to stdout."""
        header = f"=== NORTH {card.type.value.upper()} CARD ({card.id}) ==="
        border = "=" * len(header)

        lines = [
            border,
            header,
            f"Task ID: {card.task_id}",
            f"Agent:   {card.agent}",
            f"Title:   {card.title}",
            f"Message: {card.message}",
        ]

        if card.type == CardType.QUESTION and card.options:
            lines.append("Options:")
            for i, opt in enumerate(card.options, 1):
                lines.append(f"  [{i}] {opt}")

        lines.extend([
            f"Status:  {card.status}",
            border,
        ])

        output = "\n".join(lines) + "\n"
        sys.stdout.write(output)
        sys.stdout.flush()
