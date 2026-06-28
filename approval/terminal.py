"""Terminal-backed implementation of the Notifier interface.

Useful in development and testing. Prints formatted cards to the terminal.
"""

from __future__ import annotations

import asyncio
import os
import sys

from approval.base import Notifier
from approval.models import Card, CardType


class TerminalNotifier(Notifier):
    """Prints Card details directly to standard output."""

    async def notify(self, card: Card) -> None:
        """Render the card and write it to the log file or stdout.

        The write is off-loaded to a worker thread so the blocking file append
        never stalls the event loop.
        """
        await asyncio.to_thread(self._write, self._render(card))

    @staticmethod
    def _render(card: Card) -> str:
        """Build the boxed text representation of a card (pure, no I/O)."""
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

        lines.extend([f"Status:  {card.status}", border])
        return "\n".join(lines) + "\n"

    @staticmethod
    def _write(output: str) -> None:
        """Append to NORTH_LOG_FILE when set, else write to stdout."""
        log_file = os.environ.get("NORTH_LOG_FILE", "").strip()
        if log_file:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(output)
        else:
            sys.stdout.write(output)
            sys.stdout.flush()
