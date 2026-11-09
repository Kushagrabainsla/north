"""Telegram gateway — polls Telegram for messages and routes them into north.

Runs as a background asyncio task inside the north orchestrator process.
Uses the webhook endpoint (POST /orchestrator/webhooks/telegram) to submit
tasks, which reuses the existing auth and routing pipeline.

Set up in the dashboard (onboarding, or Settings > Connections) or with `north setup`;
until a bot token and a valid allowlist are set, it waits.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import CallbackQuery, Message
from aiogram.utils.backoff import BackoffConfig

from config.approval_mode import ApprovalMode, require_approval_mode
from config.settings import settings
from gateways.telegram_api import (
    HTTP_TIMEOUT as _HTTP_TIMEOUT,
)
from gateways.telegram_api import (
    approval_keyboard,
    bot_url,
    is_option_choice,
    parse_approval_callback,
    referenced_card,
    send_message,
    tapped_label,
    within_telegram_limit,
)
from gateways.telegram_outbox import TelegramOutbox

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 2.0  # seconds before listening again after polling itself stopped
_LONG_POLL_SECONDS = 25
_BACKOFF = BackoffConfig(min_delay=1.0, max_delay=30.0, factor=1.5, jitter=0.1)
# How often a gateway that is not set up looks again. The dashboard saves the bot
# into the in-memory settings, so the check is cheap and needs no restart.
_SETUP_RECHECK_S = 5.0
# How the gateway waits for a task's answer. It used to give up after 90 s, which a
# coding task (an agent's edits, north's test run, a card to land it) never beat, so
# the answer never reached the chat. A task can wait on a card as long as the user
# takes, so the wait follows the task, checking less often as it goes on.
_TASK_POLL_INTERVAL = 1.0  # first gap between checks, in seconds
_TASK_POLL_MAX_INTERVAL = 15.0  # the gap grows to this
_TASK_POLL_BACKOFF = 1.5
_TASK_WAIT_LIMIT_S = 24 * 3600  # past a day, say so and stop holding the chat
_MAX_RETRIES = 3

_MAX_LISTED_TASKS = 5
_MAX_LISTED_DECISIONS = 5


def _mode_label(mode: str) -> str:
    """The mode as Telegram shows it: YOLO carries a badge, since it says yes to everything."""
    if mode == ApprovalMode.YOLO:
        return f"⚠️ **YOLO** `{mode}` — every approval is yes"
    return f"`{mode}`"


def _decision_text(card: dict) -> str:
    """One decided card in Telegram Markdown, from the same fields the approvals page reads."""
    chosen = f' "{card["chosen_option"]}"' if card.get("type") == "question" and card.get("chosen_option") else ""
    who = f" by {card['decided_by_label']}" if card.get("decided_by_label") else ""
    lines = [f"• **{card.get('title', '')}**: {card.get('status', '')}{chosen}{who}", *_why_text(card)]
    prior = card.get("overruled")
    if prior:
        by = f" by {prior['decided_by_label']}" if prior.get("decided_by_label") else ""
        lines.append(f"  You overruled north, which had {prior.get('status', '')}{by}")
        lines += [f"  {line}" for line in _why_text(prior)]
    return "\n".join(lines)


def _why_text(decision: dict) -> list[str]:
    lines = [f"  _{decision['reason']}_"] if decision.get("reason") else []
    for ref in decision.get("memory_used") or []:
        text = f": {ref['text']}" if ref.get("text") else ""
        lines.append(f"  · {ref.get('label', ref.get('kind', ''))}{text}")
    return lines


@dataclass(frozen=True)
class _Reply:
    """Where an answer goes: the chat, and the message it answers."""

    chat_id: int
    message_id: int


def _is_allowed(chat_id: int | None, from_id: int | None) -> bool:
    """Whether this chat or user is on the allowlist. An empty list allows nobody."""
    allowed = settings.parsed_telegram_allowed_chat_ids
    return (chat_id is not None and chat_id in allowed) or (from_id is not None and from_id in allowed)


def _is_allowed_sender(msg: dict) -> bool:
    return _is_allowed(msg["chat"]["id"], msg.get("from", {}).get("id"))


_LEDGER_POLL_LIMIT = 50
# Ledger actions that carry a finished task's answer, most authoritative first.
_COMPLETED_ACTIONS = ("task_synthesis", "task_completed", "task_completed_with_failures")
_TERMINAL_STATUSES = ("failed", "cancelled")
# The task's own end, the only thing besides its answer that ends the wait. A failed
# step on the way is not one: the task carries on, and the chat was told it had failed.
_ENDED_ACTIONS = ("task_cancelled", "task_failed", "task_needs_attention")


class _TaskNotFound(Exception):
    """The orchestrator has no record of the task being polled."""


def _unprompted_approval_card(entry: dict, already_prompted: set[str]) -> str:
    """The card id this entry is asking about, or "" when it is not a fresh ask."""
    if entry.get("action") != "approval_required":
        return ""
    card_id = entry.get("card_id") or ""
    return "" if card_id in already_prompted else card_id


def _finished_output(entries: list[dict], task_id: str, poll: int) -> str | None:
    """The task's answer once the ledger shows it has finished, else None."""
    for entry in entries:  # most-recent-first
        action = entry.get("action", "")
        if action in _COMPLETED_ACTIONS and entry.get("output"):
            logger.info("Task %s found %s output at poll %d", task_id, action, poll)
            return entry["output"]

    for entry in entries:
        status = (entry.get("status") or "").lower()
        output = entry.get("output")
        if status in _TERMINAL_STATUSES and entry.get("action") in _ENDED_ACTIONS:
            logger.warning("Task %s terminal status=%s at poll %d", task_id, status, poll)
            return f"Task {status}: {output}" if output else f"Task {status}."

    # Its latest agent's answer, once the task has ended. Taken as soon as any agent
    # answered, a task with more to do (a later stage, a message you sent mid-task)
    # sent the chat its first answer and never the rest.
    if any(entry.get("action") in _COMPLETED_ACTIONS for entry in entries):
        for entry in entries:
            if entry.get("action") == "agent_completed" and entry.get("output"):
                logger.info("Task %s found agent_completed output at poll %d", task_id, poll)
                return entry["output"]
    return None


def _headers() -> dict[str, str]:
    # Need both headers: X-Webhook-Secret for the webhook endpoint,
    # X-North-Secret for task-status polling (global Depends).
    from config.security import load_secret

    secret = load_secret()
    return {
        "Content-Type": "application/json",
        "X-Webhook-Secret": secret,
        "X-North-Secret": secret,
    }


class TelegramGateway:
    """Polls Telegram for new messages and posts results back."""

    def __init__(self, orchestrator_base: str = "http://127.0.0.1:8000") -> None:
        self._orchestrator_base = orchestrator_base
        self._outbox = TelegramOutbox()
        self._pending: dict[tuple[int, int], dict] = {}  # (chat_id, message_id) -> {message_id, text, task_id}
        self._tasks: set[asyncio.Task] = set()
        self._http = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
        self._running = False

    async def start(self) -> None:
        if not settings.telegram_bot_token:
            logger.warning("NORTH_TELEGRAM_BOT_TOKEN not set — Telegram gateway disabled")
            return
        self._running = True
        logger.info("Telegram gateway started (bot token configured)")

    async def stop(self) -> None:
        self._running = False
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self._http.aclose()
        logger.info("Telegram gateway stopped")

    def _track(self, work: Coroutine[Any, Any, None]) -> None:
        """Run *work* in its own task so one message cannot hold up the next, keeping a strong reference."""
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _on_message(self, message: Message) -> None:
        self._track(self._process_message(message.model_dump(mode="json", by_alias=True, exclude_none=True)))

    async def _on_callback(self, callback: CallbackQuery) -> None:
        self._track(self._process_callback_query(callback.model_dump(mode="json", by_alias=True, exclude_none=True)))

    async def _stop_when_bot_changes(self, dispatcher: Dispatcher, token: str) -> None:
        """End polling once this bot is no longer the one set up, or the gateway is stopping."""
        while self._running and settings.telegram_ready and settings.telegram_bot_token == token:
            await asyncio.sleep(_SETUP_RECHECK_S)
        with contextlib.suppress(RuntimeError):  # it was not polling yet
            await dispatcher.stop_polling()

    async def _poll(self, token: str) -> None:
        """Listen for this bot's messages. aiogram long-polls and backs off while Telegram cannot be reached."""
        dispatcher = Dispatcher()
        dispatcher.message.register(self._on_message)
        dispatcher.callback_query.register(self._on_callback)
        watcher = asyncio.create_task(self._stop_when_bot_changes(dispatcher, token))
        try:
            await dispatcher.start_polling(
                Bot(token),
                polling_timeout=_LONG_POLL_SECONDS,
                backoff_config=_BACKOFF,
                allowed_updates=["message", "callback_query"],
                handle_signals=False,
            )
        finally:
            watcher.cancel()

    async def _send_message(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        reply_markup: dict | None = None,
    ) -> dict | None:
        """Send a message to a Telegram chat."""
        return await send_message(self._http, chat_id, text, reply_to=reply_to, reply_markup=reply_markup)

    async def _edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
    ) -> bool:
        """Edit an existing Telegram message."""
        payload: dict = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            resp = await self._http.post(bot_url("editMessageText"), json=payload)
            resp.raise_for_status()
            return True
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400 and payload.get("parse_mode"):
                payload.pop("parse_mode", None)
                try:
                    resp = await self._http.post(bot_url("editMessageText"), json=payload)
                    resp.raise_for_status()
                    return True
                except httpx.RequestError:
                    pass
            logger.error("Failed to edit Telegram message %s: %s", message_id, exc)
        except httpx.RequestError as exc:
            logger.error("Failed to edit Telegram message %s: %s", message_id, exc)
        return False

    async def _answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        """Acknowledge a callback query from an inline keyboard button."""
        payload: dict = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        try:
            await self._http.post(bot_url("answerCallbackQuery"), json=payload)
        except httpx.RequestError as exc:
            logger.debug("Failed to answer callback query %s: %s", callback_query_id, exc)

    async def _respond_approval(self, card_id: str, decision: str, chosen_option: str = "") -> bool:
        """Submit an approval response to the orchestrator."""
        url = f"{self._orchestrator_base}/orchestrator/approval/respond"
        payload = {
            "card_id": card_id,
            "decision": decision,
            "chosen_option": chosen_option or ("Approve" if decision == "approved" else "Reject"),
        }
        try:
            resp = await self._http.post(url, json=payload, headers=_headers())
            return resp.status_code in (200, 204)
        except httpx.RequestError as exc:
            logger.error("Failed to submit approval response to orchestrator: %s", exc)
            return False

    async def _cancel_task(self, target_id: str) -> bool:
        """Cancel a running task in the orchestrator."""
        url = f"{self._orchestrator_base}/orchestrator/cancel/{target_id}"
        try:
            resp = await self._http.post(url, headers=_headers())
            return resp.status_code == 200
        except httpx.RequestError as exc:
            logger.error("Failed to cancel task %s: %s", target_id, exc)
            return False

    async def _get_settings(self) -> dict | None:
        """Fetch orchestrator settings."""
        url = f"{self._orchestrator_base}/orchestrator/settings"
        try:
            resp = await self._http.get(url, headers=_headers())
            if resp.status_code == 200:
                return resp.json()
        except httpx.RequestError:
            pass
        return None

    async def _update_settings(self, new_settings: dict) -> dict | None:
        """Update orchestrator settings."""
        url = f"{self._orchestrator_base}/orchestrator/settings"
        try:
            resp = await self._http.post(url, json=new_settings, headers=_headers())
            if resp.status_code == 200:
                return resp.json()
        except httpx.RequestError:
            pass
        return None

    async def _send_limits(self, chat_id: int, reply_to: int | None = None) -> None:
        """Send the current rate-limit / cooldown status as Markdown."""
        from inference.rate_limit_status import format_status_markdown

        text = format_status_markdown(settings.north_home / "rate_limit_status.json")
        await self._send_message(chat_id, text, reply_to=reply_to)

    async def _send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        """Show a typing indicator in the chat."""
        with contextlib.suppress(httpx.RequestError):
            await self._http.post(bot_url("sendChatAction"), json={"chat_id": chat_id, "action": action})

    async def _typing_keepalive(self, chat_id: int, stop_event: asyncio.Event) -> None:
        """Periodically refresh the typing status until stop_event is set."""
        while not stop_event.is_set():
            await self._send_chat_action(chat_id, "typing")
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=4.0)

    async def _submit_task(self, text: str) -> dict | None:
        """Submit a prompt to north via the webhook endpoint, in the folder `/workspace` chose."""
        url = f"{self._orchestrator_base}/orchestrator/webhooks/telegram"
        body = {"prompt": text}
        if workspace := _chosen_workspace():
            body["workspace"] = workspace
        try:
            resp = await self._http.post(
                url,
                json=body,
                headers=_headers(),
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            logger.error("Failed to submit task to north: %s", exc)
            return None

    async def _waiting_questions(self) -> list[dict]:
        """Every question waiting for you to answer."""
        try:
            resp = await self._http.get(f"{self._orchestrator_base}/web/api/approvals", headers=_headers())
        except httpx.RequestError as exc:
            logger.error("Failed to read the cards waiting on you: %s", exc)
            return []
        if resp.status_code != 200:
            return []
        return [card for card in resp.json() if card.get("status") == "pending" and card.get("type") == "question"]

    async def _question_waiting_in(self, task_id: str) -> str | None:
        """The id of the question *task_id* is waiting for you to answer, or None."""
        waiting = [card for card in await self._waiting_questions() if card.get("task_id") == task_id]
        return max(waiting, key=lambda card: card.get("created_at") or "")["id"] if waiting else None

    async def _question_replied_to(self, msg: dict) -> str | None:
        """The waiting question this message is a reply to, or None."""
        reference = referenced_card((msg.get("reply_to_message") or {}).get("text") or "")
        if not reference:
            return None
        return next((card["id"] for card in await self._waiting_questions() if card["id"].startswith(reference)), None)

    async def _send_into_task(self, task_id: str, text: str) -> bool:
        """Send *text* into a running task; False when it has finished or north cannot be reached."""
        try:
            resp = await self._http.post(
                f"{self._orchestrator_base}/orchestrator/steer",
                json={"task_id": task_id, "instruction": text},
                headers=_headers(),
            )
        except httpx.RequestError as exc:
            logger.error("Failed to send a message into task %s: %s", task_id, exc)
            return False
        return resp.status_code == 200

    async def _get_task_result(self, task_id: str, chat_id: int | None = None) -> str | None:
        """Poll the ledger for the completed agent's output, sending interactive approval cards if needed."""
        prompted_cards: set[str] = set()
        deadline = time.monotonic() + _TASK_WAIT_LIMIT_S
        interval = _TASK_POLL_INTERVAL
        poll = 0
        while time.monotonic() < deadline:
            try:
                entries = await self._ledger_entries(task_id)
            except _TaskNotFound:
                return "Task not found."

            if chat_id is not None:
                for entry in entries:
                    card_id = _unprompted_approval_card(entry, prompted_cards)
                    if card_id:
                        prompted_cards.add(card_id)
                        await self._send_approval_card(chat_id, task_id, entry)

            output = _finished_output(entries, task_id, poll)
            if output is not None:
                return output
            await asyncio.sleep(interval)
            interval = min(interval * _TASK_POLL_BACKOFF, _TASK_POLL_MAX_INTERVAL)
            poll += 1

        logger.error("Task %s still had no answer after %ds", task_id, _TASK_WAIT_LIMIT_S)
        return f"Task `{task_id}` is still running after a day. Its result will be in north when it finishes."

    async def _ledger_entries(self, task_id: str) -> list[dict]:
        """This task's ledger entries; empty when the orchestrator cannot be reached."""
        try:
            resp = await self._http.get(
                f"{self._orchestrator_base}/orchestrator/ledger",
                params={"task_id": task_id, "limit": _LEDGER_POLL_LIMIT},
                headers=_headers(),
            )
        except httpx.RequestError:
            return []
        if resp.status_code == 404:
            raise _TaskNotFound(task_id)
        if resp.status_code != 200:
            return []
        return resp.json()

    async def _send_approval_card(self, chat_id: int, task_id: str, entry: dict) -> None:
        card_id = entry.get("card_id") or ""
        await self._send_message(
            chat_id,
            f"⚠️ **Approval Required**\n\n"
            f"Task `{task_id}` requires your confirmation to proceed:\n"
            f"_{entry.get('message', 'Confirm action')}_",
            reply_markup=approval_keyboard(card_id),
        )

    async def _download_file(self, file_id: str) -> bytes | None:
        """Download a file from Telegram by its file_id."""
        try:
            resp = await self._http.get(bot_url("getFile"), params={"file_id": file_id})
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                return None
            file_path = data["result"]["file_path"]
            file_url = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file_path}"
            resp = await self._http.get(file_url)
            resp.raise_for_status()
            return resp.content
        except httpx.RequestError as exc:
            logger.error("Failed to download Telegram file %s: %s", file_id, exc)
            return None

    async def _transcribe_audio(self, audio_bytes: bytes) -> str | None:
        """Send audio bytes to north's /transcribe endpoint and return the text."""
        url = f"{self._orchestrator_base}/orchestrator/transcribe"
        try:
            resp = await self._http.post(
                url,
                content=audio_bytes,
                headers={
                    "Content-Type": "audio/ogg",
                    **_headers(),
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("text", "")
        except httpx.RequestError as exc:
            logger.error("Failed to transcribe audio: %s", exc)
            return None

    async def _process_callback_query(self, cb: dict) -> None:
        """Handle inline button clicks (approvals, dismissals)."""
        cb_id = cb.get("id", "")
        from_id = cb.get("from", {}).get("id")
        msg = cb.get("message", {})
        chat_id = msg.get("chat", {}).get("id")
        data = cb.get("data", "")

        if not _is_allowed(chat_id, from_id):
            await self._answer_callback_query(cb_id, text="⛔ Unauthorized")
            return

        parsed = parse_approval_callback(data)
        if parsed is None:
            await self._answer_callback_query(cb_id)
            return

        decision, card_id = parsed
        if is_option_choice(decision):
            # The button holds the option's place on the card; its label is the option.
            decision = tapped_label(msg, data)
            if not decision:
                await self._answer_callback_query(cb_id, text="❌ Could not read that button; answer on the web.")
                return
        if not await self._respond_approval(card_id, decision):
            await self._answer_callback_query(cb_id, text="❌ Failed to record decision (already resolved or error)")
            return

        status_icon = "❌" if decision == "rejected" else "✅"
        msg_id = msg.get("message_id")
        if chat_id and msg_id:
            orig_text = msg.get("text", "Approval Request")
            new_text = f"{orig_text}\n\n{status_icon} **Decision:** {decision.capitalize()} (via Telegram)"
            await self._edit_message_text(chat_id, msg_id, new_text, reply_markup={"inline_keyboard": []})
        await self._answer_callback_query(cb_id, text=f"{status_icon} Decision recorded: {decision}")

    async def _process_message(self, msg: dict) -> None:
        """Process one incoming Telegram message."""
        chat = _Reply(chat_id=msg["chat"]["id"], message_id=msg["message_id"])
        if not _is_allowed_sender(msg):
            logger.warning(
                "Unauthorized Telegram message from chat_id=%s from_id=%s",
                chat.chat_id,
                msg.get("from", {}).get("id"),
            )
            await self._reply(
                chat,
                "⛔ Unauthorized: this Telegram account/chat is not on the allowed list for this North instance.",
            )
            return

        text = await self._spoken_or_written_text(chat, msg)
        if not text:
            return
        # A reply to a question answers it, whichever task asked and however the answer starts.
        replied_to = await self._question_replied_to(msg)
        if replied_to is not None and await self._respond_approval(replied_to, "answered", text):
            await self._reply(chat, f"✅ Answered: {text}")
            return
        if text.startswith("/") and not await self._is_answer(chat, text):
            await self._run_command(chat, text)
            return
        await self._run_task(chat, text)

    async def _is_answer(self, chat: _Reply, text: str) -> bool:
        """Whether *text*, which starts with a slash, answers the question this chat's task is asking.

        `/Users/me/notes` is an answer to "which folder?". A real command is always a command.
        """
        if text.split()[0] in _COMMAND_HANDLERS:
            return False
        running = self._pending_task_for(chat.chat_id)
        return bool(running) and await self._question_waiting_in(running) is not None

    async def _reply(self, chat: _Reply, text: str) -> None:
        await self._send_message(chat.chat_id, text, reply_to=chat.message_id)

    async def _spoken_or_written_text(self, chat: _Reply, msg: dict) -> str:
        """The message's text, transcribing a voice note first when that is what it is."""
        voice = msg.get("voice")
        if not voice:
            return msg.get("text", "").strip()

        await self._send_chat_action(chat.chat_id, "record_voice")
        file_id = voice.get("file_id")
        if not file_id:
            await self._reply(chat, "❌ Could not read voice message.")
            return ""
        audio_bytes = await self._download_file(file_id)
        if not audio_bytes:
            await self._reply(chat, "❌ Failed to download voice message.")
            return ""
        await self._send_chat_action(chat.chat_id, "typing")
        transcribed = await self._transcribe_audio(audio_bytes)
        if not transcribed:
            await self._reply(chat, "❌ Could not transcribe voice message.")
            return ""
        return transcribed

    async def _run_command(self, chat: _Reply, text: str) -> None:
        """Run a slash command, or say that it is not one.

        An unknown command used to be dropped without a word: on Oct 25 north
        itself told you to send `/record_audio 5`, which does not exist, and the
        chat stayed silent while you waited for a recording.
        """
        parts = text.split()
        handler = _COMMAND_HANDLERS.get(parts[0])
        if handler is None:
            await self._reply(
                chat,
                f"`{parts[0]}` is not a north command, so nothing ran. To ask north for something, "
                "send it as a plain message, without the slash. /help lists the commands.",
            )
            return
        await handler(self, chat, parts[1:])

    async def _command_help(self, chat: _Reply, args: list[str]) -> None:
        await self._reply(
            chat,
            "👋 **Welcome to North** — Your Autonomous Assistant\n\n"
            "Send any message or voice note to execute tasks.\n\n"
            "**Available Controls:**\n"
            "  • `/status` — View active tasks & orchestrator status\n"
            "  • `/cancel` — Cancel the currently running task\n"
            f"  • `/autonomy` — View or set approval mode (`/autonomy {'|'.join(ApprovalMode)}`)\n"
            "  • `/decisions` — Recent decisions: who decided, why, and the memory it used\n"
            "  • `/limits` — Show provider/model rate-limit & cooldown status\n"
            "  • `/workspace` — The folder tasks work in (`/workspace <path>` to switch, `/workspace reset`)\n"
            "  • `/help` — Show this command reference",
        )

    async def _command_workspace(self, chat: _Reply, args: list[str]) -> None:
        """Show or choose the folder tasks from this chat work in, as the dashboard's picker does for a chat."""
        if not args:
            chosen = _chosen_workspace()
            where = f"`{chosen}`" if chosen else _own_folder()
            await self._reply(chat, f"📁 Tasks from Telegram work in {where}.\nSwitch with `/workspace <path>`.")
            return
        if args == ["reset"]:
            _workspace_file().unlink(missing_ok=True)
            await self._reply(chat, f"📁 Back to {_own_folder()}.")
            return
        raw = " ".join(args)
        try:
            path = Path(raw).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            await self._reply(chat, f"❌ `{raw}` does not exist.")
            return
        if not path.is_dir():
            await self._reply(chat, f"❌ `{raw}` is not a folder.")
            return
        _workspace_file().write_text(str(path), encoding="utf-8")
        note = "" if _in_git_repo(path) else "\nIt is not a git repository, so coding tasks cannot run there."
        await self._reply(chat, f"📁 Tasks from Telegram now work in `{path}`.{note}")

    async def _command_limits(self, chat: _Reply, args: list[str]) -> None:
        await self._send_limits(chat.chat_id, reply_to=chat.message_id)

    async def _command_status(self, chat: _Reply, args: list[str]) -> None:
        try:
            resp = await self._http.get(f"{self._orchestrator_base}/orchestrator/tasks", headers=_headers())
        except httpx.RequestError as exc:
            await self._reply(chat, f"❌ Connection error: {exc}")
            return
        if resp.status_code != 200:
            await self._reply(chat, "⚠️ Could not retrieve tasks from orchestrator.")
            return
        tasks = resp.json()
        if not tasks:
            await self._reply(chat, "🟢 **Status:** Idle — No active tasks running.")
            return
        lines = [f"🔄 **Active Tasks ({len(tasks)}):**"]
        lines.extend(
            f"  • `{task.get('task_id')}`: {task.get('status')} ({task.get('agent', 'orch')})"
            for task in tasks[:_MAX_LISTED_TASKS]
        )
        await self._reply(chat, "\n".join(lines))

    async def _command_cancel(self, chat: _Reply, args: list[str]) -> None:
        target_task = args[0] if args else self._pending_task_for(chat.chat_id)
        if not target_task:
            await self._reply(chat, "ℹ️ No running tasks found to cancel.")
            return
        if await self._cancel_task(target_task):
            await self._reply(chat, f"🛑 Task `{target_task}` cancelled.")
        else:
            await self._reply(chat, f"❌ Failed to cancel task `{target_task}`.")

    def _pending_task_for(self, chat_id: int) -> str:
        """The task this chat is waiting on, or "" when it is waiting on none."""
        for (pending_chat_id, _), item in self._pending.items():
            if pending_chat_id == chat_id:
                return item.get("task_id", "")
        return ""

    async def _command_autonomy(self, chat: _Reply, args: list[str]) -> None:
        if not args:
            current = await self._get_settings()
            if current is None:
                await self._reply(chat, "❌ Could not read the approval mode from north.")
                return
            options = "\n".join(
                f"  • `{option['value']}` — {option['description']}" for option in current.get("autonomy_options", [])
            )
            await self._reply(
                chat,
                f"⚙️ Approval mode: {_mode_label(current['autonomy'])}\n\n{options}\n\n"
                "Use `/autonomy <mode>` to change.",
            )
            return

        try:
            wanted = require_approval_mode(args[0])
        except ValueError as exc:
            await self._reply(chat, f"⚠️ {exc}")
            return
        updated = await self._update_settings({"autonomy": wanted.value})
        if updated is None:
            await self._reply(chat, "❌ Failed to update approval mode.")
            return
        await self._reply(chat, f"✅ Approval mode: {_mode_label(updated['autonomy'])}")

    async def _command_decisions(self, chat: _Reply, args: list[str]) -> None:
        """Recent decisions as the approvals page shows them: who decided, why, and the memory it used."""
        try:
            resp = await self._http.get(f"{self._orchestrator_base}/web/api/approvals", headers=_headers())
        except httpx.RequestError as exc:
            await self._reply(chat, f"❌ Connection error: {exc}")
            return
        if resp.status_code != 200:
            await self._reply(chat, "⚠️ Could not read decisions from north.")
            return
        decided = [card for card in resp.json() if card.get("status") != "pending"][:_MAX_LISTED_DECISIONS]
        if not decided:
            await self._reply(chat, "🧭 Nothing decided yet.")
            return
        body = "\n\n".join(_decision_text(card) for card in decided)
        await self._reply(chat, within_telegram_limit(f"🧭 **Recent decisions**\n\n{body}\n\nOverrule one on the web."))

    async def _run_task(self, chat: _Reply, text: str) -> None:
        """Submit a message to north and reply with whatever it produces.

        Sent while this chat's task still works, it goes into that task instead (CODING_STYLE §15.4),
        whose answer this chat is already waiting for.
        """
        await self._send_chat_action(chat.chat_id)
        running = self._pending_task_for(chat.chat_id)
        if running:
            # The task is asking you something: your reply is the answer. Sent into the
            # task instead, it waited behind the very question it answered.
            question = await self._question_waiting_in(running)
            if question is not None and await self._respond_approval(question, "answered", text):
                await self._reply(chat, f"✅ Answered: {text}")
                return
            if await self._send_into_task(running, text):
                await self._reply(chat, "↳ Added to the task I'm working on; its answer will cover it.")
                return

        result = await self._submit_task(text)
        if result is None:
            await self._reply(chat, "❌ north could not start this task. Check `north status` or north.log.")
            return
        task_id = result.get("task_id", "")
        if not task_id:
            await self._reply(chat, "❌ North did not return a task ID.")
            return

        _remember_wait(chat, text, task_id)
        await self._answer(chat, text, task_id)

    async def _answer(self, chat: _Reply, text: str, task_id: str) -> None:
        """Wait for the task and reply with what it produced, then forget the wait.

        Forgotten only once the reply is sent: a wait cut short by a shutdown stays saved, and
        `resume_waits` picks it up when the gateway starts again.
        """
        pending_key = (chat.chat_id, chat.message_id)
        self._pending[pending_key] = {
            "message_id": chat.message_id,
            "text": text,
            "task_id": task_id,
        }
        try:
            output = await self._await_result_while_typing(task_id, chat.chat_id)
        finally:
            self._pending.pop(pending_key, None)

        text = within_telegram_limit(output or f"✅ Task submitted (ID: `{task_id}`). Check north for results.")
        if await self._send_message(chat.chat_id, text, reply_to=chat.message_id) is None:
            await self._outbox.add(chat.chat_id, text, reply_to=chat.message_id)  # goes out when Telegram is back
        _forget_wait(chat)

    def resume_waits(self) -> int:
        """Go back to waiting on the tasks a restart cut off, so their chats still hear how they ended."""
        waits = _saved_waits()
        for wait in waits.values():
            chat = _Reply(chat_id=int(wait["chat_id"]), message_id=int(wait["message_id"]))
            task = asyncio.create_task(self._answer(chat, str(wait.get("text", "")), str(wait["task_id"])))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        if waits:
            logger.info("Telegram gateway resumed waiting on %d task(s) from before a restart", len(waits))
        return len(waits)

    async def _await_result_while_typing(self, task_id: str, chat_id: int) -> str | None:
        """Wait for a task's output, holding the typing indicator up while it runs."""
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(self._typing_keepalive(chat_id, stop_typing))
        try:
            return await self._get_task_result(task_id, chat_id=chat_id)
        finally:
            stop_typing.set()
            typing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await typing_task

    async def _wait_until_ready(self) -> None:
        """Wait for a bot token and a valid allowlist, saying once per reason why it waits.

        It fails closed: without a valid allowlist anyone who finds the bot could run tasks.
        """
        said = ""
        while not settings.telegram_ready:
            reason = settings.telegram_allowlist_problem if settings.telegram_bot_token else "no bot token"
            if reason != said:
                said = reason
                logger.log(
                    logging.ERROR if settings.telegram_bot_token else logging.INFO,
                    "Telegram gateway waiting: %s. Set it up in the dashboard under Settings.",
                    reason,
                )
            await asyncio.sleep(_SETUP_RECHECK_S)

    async def run(self) -> None:
        """Main polling loop — background task entrypoint.

        Waits until Telegram is set up, so a bot saved on the dashboard starts
        without a restart, and goes back to waiting if it is disconnected.
        """
        await self._wait_until_ready()
        await self.start()
        self.resume_waits()
        logger.info("Telegram gateway polling loop started")

        while self._running:
            token = ""
            try:
                await self._wait_until_ready()
                token = settings.telegram_bot_token
                await self._poll(token)
            except asyncio.CancelledError:
                break
            except TelegramUnauthorizedError:
                logger.error("Telegram refused the bot token; waiting for a new one. Set it in Settings.")
                await self._wait_for_another_bot(token)
            except Exception:
                logger.exception("Telegram polling stopped")
                await asyncio.sleep(_POLL_INTERVAL)

        await self.stop()

    async def _wait_for_another_bot(self, token: str) -> None:
        while self._running and settings.telegram_bot_token == token:
            await asyncio.sleep(_SETUP_RECHECK_S)


# Every slash command the gateway answers. Anything else is left to north itself
# to read as an ordinary message.
def _own_folder() -> str:
    """Where a task works when no folder was chosen for it (CODING_STYLE §16.1.1)."""
    return f"each task's own folder, `{settings.north_home / 'tasks'}/<task id>/`"


def _workspace_file() -> Path:
    """Where `/workspace` keeps its choice, so a restart does not lose it."""
    return settings.north_home / "telegram_workspace.txt"


def _chosen_workspace() -> str:
    """The folder `/workspace` chose, or "" for the server's default."""
    try:
        return _workspace_file().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _waits_file() -> Path:
    """The chats waiting on a task's answer, kept on disk so a restart does not leave them hanging."""
    return settings.north_home / "telegram_waits.json"


def _saved_waits() -> dict[str, dict]:
    try:
        waits = json.loads(_waits_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return waits if isinstance(waits, dict) else {}


def _write_waits(waits: dict[str, dict]) -> None:
    path = _waits_file()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(waits), encoding="utf-8")
    tmp.replace(path)


def _remember_wait(chat: _Reply, text: str, task_id: str) -> None:
    waits = _saved_waits()
    waits[f"{chat.chat_id}:{chat.message_id}"] = {
        "chat_id": chat.chat_id,
        "message_id": chat.message_id,
        "text": text,
        "task_id": task_id,
    }
    _write_waits(waits)


def _forget_wait(chat: _Reply) -> None:
    waits = _saved_waits()
    if waits.pop(f"{chat.chat_id}:{chat.message_id}", None) is not None:
        _write_waits(waits)


def _in_git_repo(path: Path) -> bool:
    return any((folder / ".git").exists() for folder in (path, *path.parents))


_COMMAND_HANDLERS: dict[str, Callable[[TelegramGateway, _Reply, list[str]], Awaitable[None]]] = {
    "/start": TelegramGateway._command_help,
    "/help": TelegramGateway._command_help,
    "/limits": TelegramGateway._command_limits,
    "/status": TelegramGateway._command_status,
    "/cancel": TelegramGateway._command_cancel,
    "/stop": TelegramGateway._command_cancel,
    "/autonomy": TelegramGateway._command_autonomy,
    "/decisions": TelegramGateway._command_decisions,
    "/workspace": TelegramGateway._command_workspace,
}
