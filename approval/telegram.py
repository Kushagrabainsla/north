"""Telegram-backed Notifier - how north starts a conversation.

Every other notifier answers a question the user is already watching for: the
TUI has the card inline over SSE, the terminal writes where someone is looking,
macOS pops a notification on the machine you are sitting at. None of them reach
a user who is not at the keyboard, which is exactly the user a task started by
cron has.

That was the whole of issue #2: north could run a job at 03:00 and had no way to
tell you it ran, or to ask whether it should proceed. The card went to a
terminal nobody was watching. The gateway could already *reply* to Telegram, but
only for tasks it was tracking from an inbound message - a cron-launched task
was never in that map.

This is the missing direction: a card nobody asked for, delivered to the chats
the operator has allow-listed.
"""

from __future__ import annotations

import logging

import httpx

from approval.base import Notifier
from approval.models import Card, CardType
from config.settings import settings
from gateways.telegram_api import (
    HTTP_TIMEOUT,
    approval_keyboard,
    send_message,
    within_telegram_limit,
)

logger = logging.getLogger(__name__)

_ICONS: dict[CardType, str] = {
    CardType.APPROVAL: "⚠️",
    CardType.QUESTION: "❓",
    CardType.INFORMATION: "ℹ️",
}
# How much of a card's source material to include. The posting behind a filled
# application is often longer than a Telegram message; the point here is enough
# to recognise the item, with the web UI for the rest.
_CONTEXT_PREVIEW_CHARS = 500


class TelegramNotifier(Notifier):
    """Delivers a card to the operator's Telegram chats.

    Configured by `NORTH_TELEGRAM_BOT_TOKEN` and
    `NORTH_TELEGRAM_ALLOWED_CHAT_IDS`. With either missing this notifier is
    inert and delegates to `fallback`, so an install that has never set up
    Telegram behaves exactly as it did before.
    """

    def __init__(self, fallback: Notifier, http: httpx.AsyncClient | None = None) -> None:
        self._fallback = fallback
        self._http = http or httpx.AsyncClient(timeout=HTTP_TIMEOUT)

    @property
    def configured(self) -> bool:
        return bool(settings.telegram_bot_token and settings.parsed_telegram_allowed_chat_ids)

    async def notify(self, card: Card) -> None:
        if not self.configured:
            await self._fallback.notify(card)
            return

        text = within_telegram_limit(self._render(card))
        markup = approval_keyboard(card.id, list(card.options)) if card.type is CardType.APPROVAL else None

        delivered = False
        for chat_id in sorted(settings.parsed_telegram_allowed_chat_ids):
            if await send_message(self._http, chat_id, text, reply_markup=markup) is not None:
                delivered = True

        if not delivered:
            # Telegram was configured and still did not take it. Falling through
            # to the terminal is not a real delivery, but a card that reached
            # nobody and left no trace is the failure this whole issue is about.
            logger.warning("TelegramNotifier: card %s reached no chat - falling back", card.id)
            await self._fallback.notify(card)

    @staticmethod
    def _render(card: Card) -> str:
        """The card as a Telegram message.

        Fields are included because a card can now carry work north filled in
        (#11), and "north has something for you" is not reviewable. They are
        shown read-only - editing a value happens on the web UI, and a decision
        made here applies the values as proposed.
        """
        icon = _ICONS.get(card.type, "•")
        lines = [f"{icon} **{card.title}**", ""]
        if card.message:
            lines.append(card.message)

        if card.fields:
            lines.append("")
            for field in card.fields:
                value = str(field.value)
                if len(value) > 200:
                    value = value[:200] + "…"
                lines.append(f"• *{field.display_label()}*: {value}")

        if card.context:
            preview = card.context[:_CONTEXT_PREVIEW_CHARS]
            if len(card.context) > _CONTEXT_PREVIEW_CHARS:
                preview += "…"
            lines.extend(["", "_Source:_", preview])

        if card.type is CardType.QUESTION and card.options:
            lines.extend(["", "Reply with one of: " + ", ".join(card.options)])

        footer = f"_{card.agent}_" if card.agent else ""
        if not card.blocking:
            # Says which kind of waiting this is. A guard-rail has an agent
            # frozen behind it; prepared work does not, and telling the two
            # apart is what makes it safe to answer this one tomorrow.
            footer = f"{footer} · no rush, nothing is blocked" if footer else "No rush, nothing is blocked"
        if footer:
            lines.extend(["", footer])

        return "\n".join(lines)

    async def aclose(self) -> None:
        await self._http.aclose()
