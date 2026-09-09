"""The raw Telegram send layer, shared by the gateway and the notifier.

Two things now send to Telegram: `TelegramGateway`, which replies to messages
you sent it, and `TelegramNotifier`, which delivers a card nobody asked for.
They must agree exactly on the approval button format - a button carries the
decision and the card id in its `callback_data`, and the gateway is what parses
it back. Written twice, the two copies drift and a button silently stops
resolving, which looks like an approval that was never answered.

So the wire format lives here once, and neither caller writes it.
"""

from __future__ import annotations

import logging

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot"
HTTP_TIMEOUT = 30.0

# Telegram rejects anything over 4096 characters; the rest of the budget is the
# note explaining where the full answer lives.
MAX_MESSAGE_CHARS = 3900

# `callback_data` layout for an approval button: approval:<decision>:<card_id>.
# Telegram caps callback_data at 64 bytes, which a card id fits inside.
_CALLBACK_PREFIX = "approval"


def bot_url(method: str) -> str:
    return f"{TELEGRAM_API}{settings.telegram_bot_token}/{method}"


def within_telegram_limit(output: str) -> str:
    if len(output) <= MAX_MESSAGE_CHARS:
        return output
    return output[:MAX_MESSAGE_CHARS] + "\n\n[truncated — see north for full response]"


def approval_callback_data(decision: str, card_id: str) -> str:
    """The `callback_data` a decision button carries."""
    return f"{_CALLBACK_PREFIX}:{decision}:{card_id}"


def parse_approval_callback(data: str) -> tuple[str, str] | None:
    """Read a button's `callback_data` back as (decision, card_id), or None.

    The inverse of `approval_callback_data`, kept beside it so the two cannot
    disagree about the format.
    """
    if not data.startswith(f"{_CALLBACK_PREFIX}:"):
        return None
    parts = data.split(":", 2)
    if len(parts) != 3:
        return None
    return parts[1], parts[2]


def approval_keyboard(card_id: str, options: list[str] | None = None) -> dict:
    """The inline keyboard for a decision.

    With no options this is Approve/Reject. With options - a card that offers a
    choice rather than a yes/no - each becomes its own button, so the answer
    reaching north is the option the user picked rather than a bare "approved"
    that loses which one it was.
    """
    if options:
        rows = [[{"text": o, "callback_data": approval_callback_data(o, card_id)}] for o in options]
        return {"inline_keyboard": rows}
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": approval_callback_data("approved", card_id)},
                {"text": "❌ Reject", "callback_data": approval_callback_data("rejected", card_id)},
            ]
        ]
    }


async def send_message(
    http: httpx.AsyncClient,
    chat_id: int,
    text: str,
    reply_to: int | None = None,
    reply_markup: dict | None = None,
) -> dict | None:
    """Send one message, retrying without Markdown if Telegram rejects it.

    Returns the API response, or None when the send failed - callers decide
    whether that is worth reporting. A failed notification must never raise into
    the caller: north is telling you something, and losing the telling is not a
    reason to also lose whatever produced it.
    """
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = await http.post(bot_url("sendMessage"), json=payload)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        # Markdown parsing failed (e.g. unescaped code characters), retry as plain text
        if exc.response.status_code == 400 and payload.get("parse_mode"):
            payload.pop("parse_mode", None)
            try:
                resp = await http.post(bot_url("sendMessage"), json=payload)
                resp.raise_for_status()
                return resp.json()
            except (httpx.RequestError, httpx.HTTPStatusError) as retry_exc:
                logger.error("Failed to send Telegram plain message to %s: %s", chat_id, retry_exc)
                return None
        logger.error("Failed to send Telegram message to %s: %s", chat_id, exc)
    except httpx.RequestError as exc:
        logger.error("Failed to send Telegram message to %s: %s", chat_id, exc)
    return None
