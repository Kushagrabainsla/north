"""macOS-backed implementation of the Notifier interface using `alerter`.

See docs/CODING_STYLE.md Section 6.1 and Section 16.10.
"""

from __future__ import annotations

import asyncio
import shutil
import sys

from approval.base import Notifier
from approval.models import Card, CardType
from approval.terminal import TerminalNotifier


class MacOSNotifier(Notifier):
    """Deliver alerts using the macOS Swift-based `alerter` utility.

    If `alerter` is not found on the system path, falls back gracefully to the
    `TerminalNotifier` so development on non-macOS or unconfigured systems remains smooth.
    """

    def __init__(self, secret: str = "") -> None:
        self._secret = secret
        self._terminal_fallback = TerminalNotifier()

    async def notify(self, card: Card) -> None:
        """Post a macOS notification.

        If `alerter` is available, spawns it. Otherwise, falls back to printing the card
        to standard output.  The card is already registered in ApprovalStore by the
        Orchestrator before this method is called.
        """
        alerter_path = shutil.which("alerter")
        if not alerter_path:
            import logging

            logging.getLogger(__name__).warning(
                "alerter not found on PATH — falling back to terminal notifier for card %s", card.id
            )
            await self._terminal_fallback.notify(card)
            return

        cmd = [
            alerter_path,
            "-title",
            f"north: {card.title}",
            "-message",
            card.message,
            "-group",
            f"north-{card.task_id}",
            "-sender",
            "com.apple.Terminal",  # standard notification sender ID
        ]

        if card.type == CardType.APPROVAL:
            cmd.extend(["-actions", "Approve,Reject", "-closeLabel", "Cancel"])
        elif card.type == CardType.QUESTION and card.options:
            cmd.extend(["-actions", ",".join(card.options), "-closeLabel", "Cancel"])
        else:
            cmd.extend(["-closeLabel", "Close"])

        # Run non-blocking using asyncio.subprocess
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # In production, the card response will be handled asynchronously
            # by the callback server when user interacts with macOS alert,
            # but we can also capture standard exit if the process waits.
            # To stay non-blocking, we do not await proc.communicate() here,
            # instead we let it execute in the background.
            asyncio.create_task(self._wait_and_handle_exit(proc, card))
        except Exception as e:
            # Fail silently to terminal fallback if subprocess launch fails
            sys.stderr.write(f"WARNING: Failed to launch macOS alerter: {e}\n")
            await self._terminal_fallback.notify(card)

    async def _wait_and_handle_exit(self, proc: asyncio.subprocess.Process, card: Card) -> None:
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout:
            stdout.decode("utf-8").strip()
            # If the user clicked a valid action button, we can relay this
            # back to the orchestrator if a callback server is active.
            # Standard callback handling is done in callback_server.py.
