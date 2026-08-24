from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from typing import TYPE_CHECKING

from approval.base import Notifier
from approval.models import ApprovalDecision, Card, CardType
from approval.terminal import TerminalNotifier
from utils.tasks import spawn

if TYPE_CHECKING:
    from approval.store import ApprovalStore

logger = logging.getLogger(__name__)


def map_alerter_action_to_decision(action: str, card_type: CardType) -> tuple[str, str]:
    """Map raw alerter button label to (status, chosen_option)."""
    norm = action.strip().lower()
    if norm in {"approve", "proceed anyway", "yes", "confirm"}:
        return ApprovalDecision.APPROVED.value, action
    if norm in {"reject", "cancel", "no", "deny"}:
        return ApprovalDecision.REJECTED.value, action
    if card_type == CardType.QUESTION:
        return ApprovalDecision.ANSWERED.value, action
    return (ApprovalDecision.APPROVED.value if norm == "ok" else ApprovalDecision.REJECTED.value), action


class MacOSNotifier(Notifier):
    """Deliver alerts using the macOS Swift-based `alerter` utility.

    If `alerter` is not found on the system path, falls back gracefully to the
    `TerminalNotifier` so development on non-macOS or unconfigured systems remains smooth.
    """

    def __init__(self, secret: str = "", store: ApprovalStore | None = None) -> None:
        self._secret = secret
        self._store = store
        self._terminal_fallback = TerminalNotifier()

    async def notify(self, card: Card) -> None:
        """Post a macOS notification.

        If `alerter` is available, spawns it. Otherwise, falls back to printing the card
        to standard output. The card is already registered in ApprovalStore by the
        Orchestrator before this method is called.
        """
        alerter_path = shutil.which("alerter")
        if not alerter_path:
            logger.warning(
                "alerter not found on PATH - falling back to terminal notifier for card %s", card.id
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
            # Handle user interaction from alerter output in the background.
            spawn(self._wait_and_handle_exit(proc, card), name=f"alerter_exit:{card.id}")
        except Exception as e:
            # Fail silently to terminal fallback if subprocess launch fails
            sys.stderr.write(f"WARNING: Failed to launch macOS alerter: {e}\n")
            await self._terminal_fallback.notify(card)

    async def _wait_and_handle_exit(self, proc: asyncio.subprocess.Process, card: Card) -> None:
        try:
            stdout, _ = await proc.communicate()
            if proc.returncode == 0 and stdout:
                action = stdout.decode("utf-8").strip()
                if action:
                    status, chosen = map_alerter_action_to_decision(action, card.type)
                    if self._store is not None:
                        self._store.resolve(card.id, status=status, chosen_option=chosen)
                        logger.info("MacOSNotifier: resolved card %s with decision %s (%s)", card.id, status, chosen)
        except Exception:
            logger.exception("MacOSNotifier: error handling alerter exit for card %s", card.id)
