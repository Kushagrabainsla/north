"""Telegram gateway — polls Telegram for messages and routes them into north.

Runs as a background asyncio task inside the north orchestrator process.
Uses the webhook endpoint (POST /orchestrator/webhooks/telegram) to submit
tasks, which reuses the existing auth and routing pipeline.

Requires ``NORTH_TELEGRAM_BOT_TOKEN`` to be set in the environment or .env.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot"
_POLL_INTERVAL = 2.0  # seconds between long-poll requests
_TASK_POLL_INTERVAL = 1.0  # seconds between checking task status
_TASK_POLL_MAX_ATTEMPTS = 90  # 90 × 1s = 90s max wait for task completion
_MAX_RETRIES = 3
_HTTP_TIMEOUT = 30.0

_MAX_LISTED_TASKS = 5
_APPROVAL_MODES = ("interactive", "auto", "autonomous")
# Telegram rejects anything over 4096 characters; the rest of the budget is the
# note explaining where the full answer lives.
_MAX_MESSAGE_CHARS = 3900


@dataclass(frozen=True)
class _Reply:
    """Where an answer goes: the chat, and the message it answers."""

    chat_id: int
    message_id: int


def _is_allowed_sender(msg: dict) -> bool:
    allowed = settings.parsed_telegram_allowed_chat_ids
    if not allowed:
        return True
    from_id = msg.get("from", {}).get("id")
    return msg["chat"]["id"] in allowed or (from_id is not None and from_id in allowed)


def _within_telegram_limit(output: str) -> str:
    if len(output) <= _MAX_MESSAGE_CHARS:
        return output
    return output[:_MAX_MESSAGE_CHARS] + "\n\n[truncated — see north for full response]"


_LEDGER_POLL_LIMIT = 50
# Ledger actions that carry a finished task's answer, most authoritative first.
_COMPLETED_ACTIONS = ("task_synthesis", "task_completed", "task_completed_with_failures")
_TERMINAL_STATUSES = ("failed", "cancelled")


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
        if status in _TERMINAL_STATUSES and entry.get("output"):
            logger.warning("Task %s terminal status=%s at poll %d", task_id, status, poll)
            return f"Task {status}: {entry['output']}"

    for entry in entries:  # fallback: a single agent finished on its own
        if entry.get("action") == "agent_completed" and entry.get("output"):
            logger.info("Task %s found agent_completed output at poll %d", task_id, poll)
            return entry["output"]
    return None



def _bot_url(method: str) -> str:
    return f"{_TELEGRAM_API}{settings.telegram_bot_token}/{method}"


def _headers() -> dict[str, str]:
    # Need both headers: X-Webhook-Secret for the webhook endpoint,
    # X-North-Secret for task-status polling (global Depends).
    from utils.security import load_secret

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
        self._offset: int = 0
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

    async def _get_updates(self) -> list[dict]:
        """Long-poll Telegram for new messages and callback queries."""
        try:
            resp = await self._http.post(
                _bot_url("getUpdates"),
                json={
                    "offset": self._offset,
                    "timeout": 25,  # long-poll (seconds)
                    "allowed_updates": ["message", "callback_query"],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                logger.warning("Telegram API returned ok=false: %s", data.get("description", ""))
                return []
            return data.get("result", [])
        except httpx.RequestError as exc:
            logger.debug("Telegram poll error: %s", exc)
            return []

    async def _send_message(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        reply_markup: dict | None = None,
    ) -> dict | None:
        """Send a message to a Telegram chat."""
        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            resp = await self._http.post(_bot_url("sendMessage"), json=payload)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            # Markdown parsing failed (e.g. unescaped code characters), retry as plain text
            if exc.response.status_code == 400 and payload.get("parse_mode"):
                payload.pop("parse_mode", None)
                try:
                    resp = await self._http.post(_bot_url("sendMessage"), json=payload)
                    resp.raise_for_status()
                    return resp.json()
                except httpx.RequestError as retry_exc:
                    logger.error("Failed to send Telegram plain message to %s: %s", chat_id, retry_exc)
            logger.error("Failed to send Telegram message to %s: %s", chat_id, exc)
        except httpx.RequestError as exc:
            logger.error("Failed to send Telegram message to %s: %s", chat_id, exc)
        return None

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
            resp = await self._http.post(_bot_url("editMessageText"), json=payload)
            resp.raise_for_status()
            return True
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400 and payload.get("parse_mode"):
                payload.pop("parse_mode", None)
                try:
                    resp = await self._http.post(_bot_url("editMessageText"), json=payload)
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
            await self._http.post(_bot_url("answerCallbackQuery"), json=payload)
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
            await self._http.post(_bot_url("sendChatAction"), json={"chat_id": chat_id, "action": action})

    async def _typing_keepalive(self, chat_id: int, stop_event: asyncio.Event) -> None:
        """Periodically refresh the typing status until stop_event is set."""
        while not stop_event.is_set():
            await self._send_chat_action(chat_id, "typing")
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=4.0)

    async def _submit_task(self, text: str) -> dict | None:
        """Submit a prompt to north via the webhook endpoint."""
        url = f"{self._orchestrator_base}/orchestrator/webhooks/telegram"
        try:
            resp = await self._http.post(
                url,
                json={"prompt": text},
                headers=_headers(),
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.RequestError as exc:
            logger.error("Failed to submit task to north: %s", exc)
            return None

    async def _get_task_result(self, task_id: str, chat_id: int | None = None) -> str | None:
        """Poll the ledger for the completed agent's output, sending interactive approval cards if needed."""
        prompted_cards: set[str] = set()
        for poll in range(_TASK_POLL_MAX_ATTEMPTS):
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
            await asyncio.sleep(_TASK_POLL_INTERVAL)

        logger.error(
            "Task %s timed out after %d polls (%ds)",
            task_id,
            _TASK_POLL_MAX_ATTEMPTS,
            _TASK_POLL_MAX_ATTEMPTS,
        )
        return "Response timed out — check north for details."

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
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "✅ Approve", "callback_data": f"approval:approved:{card_id}"},
                        {"text": "❌ Reject", "callback_data": f"approval:rejected:{card_id}"},
                    ]
                ]
            },
        )

    async def _download_file(self, file_id: str) -> bytes | None:
        """Download a file from Telegram by its file_id."""
        try:
            resp = await self._http.get(_bot_url("getFile"), params={"file_id": file_id})
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

        allowed = settings.parsed_telegram_allowed_chat_ids
        if allowed and (chat_id not in allowed and (from_id is None or from_id not in allowed)):
            await self._answer_callback_query(cb_id, text="⛔ Unauthorized")
            return

        if data.startswith("approval:"):
            # Format: approval:<decision>:<card_id>
            parts = data.split(":", 2)
            if len(parts) == 3:
                decision = parts[1]
                card_id = parts[2]
                success = await self._respond_approval(card_id, decision)
                status_icon = "✅" if decision == "approved" else "❌"
                if success:
                    msg_id = msg.get("message_id")
                    if chat_id and msg_id:
                        orig_text = msg.get("text", "Approval Request")
                        new_text = f"{orig_text}\n\n{status_icon} **Decision:** {decision.capitalize()} (via Telegram)"
                        await self._edit_message_text(chat_id, msg_id, new_text, reply_markup={"inline_keyboard": []})
                    await self._answer_callback_query(cb_id, text=f"{status_icon} Decision recorded: {decision}")
                else:
                    await self._answer_callback_query(  # noqa: E501
                        cb_id, text="❌ Failed to record decision (already resolved or error)"
                    )
                return

        await self._answer_callback_query(cb_id)

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
        if text.startswith("/"):
            await self._run_command(chat, text)
            return
        await self._run_task(chat, text)

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
        """Run a slash command, ignoring anything that is not one north answers."""
        parts = text.split()
        handler = _COMMAND_HANDLERS.get(parts[0])
        if handler is not None:
            await handler(self, chat, parts[1:])

    async def _command_help(self, chat: _Reply, args: list[str]) -> None:
        await self._reply(
            chat,
            "👋 **Welcome to North** — Your Autonomous Assistant\n\n"
            "Send any message or voice note to execute tasks.\n\n"
            "**Available Controls:**\n"
            "  • `/status` — View active tasks & orchestrator status\n"
            "  • `/cancel` — Cancel the currently running task\n"
            "  • `/autonomy` — View or set approval mode (`/autonomy interactive|auto|autonomous`)\n"
            "  • `/limits` — Show provider/model rate-limit & cooldown status\n"
            "  • `/help` — Show this command reference",
        )

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
            mode = current.get("approval_mode", "interactive") if current else settings.approval_mode
            await self._reply(chat, f"⚙️ Current approval mode: `{mode}`\nUse `/autonomy <mode>` to change.")
            return

        new_mode = args[0].lower()
        if new_mode not in _APPROVAL_MODES:
            await self._reply(chat, "⚠️ Invalid mode. Choose: `interactive`, `auto`, or `autonomous`.")
            return
        if await self._update_settings({"approval_mode": new_mode}):
            await self._reply(chat, f"✅ Approval mode updated to: `{new_mode}`")
        else:
            await self._reply(chat, "❌ Failed to update approval mode.")

    async def _run_task(self, chat: _Reply, text: str) -> None:
        """Submit a message to north and reply with whatever it produces."""
        await self._send_chat_action(chat.chat_id)

        result = await self._submit_task(text)
        if result is None:
            await self._reply(chat, "❌ Failed to connect to north.")
            return
        task_id = result.get("task_id", "")
        if not task_id:
            await self._reply(chat, "❌ North did not return a task ID.")
            return

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

        if output:
            await self._reply(chat, _within_telegram_limit(output))
        else:
            await self._reply(chat, f"✅ Task submitted (ID: `{task_id}`). Check north for results.")

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

    async def run(self) -> None:
        """Main polling loop — background task entrypoint."""
        if not settings.telegram_bot_token:
            logger.info("Telegram gateway skipped (no bot token)")
            return

        await self.start()
        logger.info("Telegram gateway polling loop started")

        while self._running:
            try:
                updates = await self._get_updates()
                for update in updates:
                    self._offset = update["update_id"] + 1
                    if "message" in update:
                        msg = update["message"]
                        # Process in its own task so we don't block the poller, retaining a strong ref
                        task = asyncio.create_task(self._process_message(msg))
                        self._tasks.add(task)
                        task.add_done_callback(self._tasks.discard)
                    elif "callback_query" in update:
                        cb = update["callback_query"]
                        task = asyncio.create_task(self._process_callback_query(cb))
                        self._tasks.add(task)
                        task.add_done_callback(self._tasks.discard)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Telegram poll iteration failed")
                await asyncio.sleep(_POLL_INTERVAL)
            else:
                if not updates:  # no updates means long-poll timed out — poll again immediately
                    continue
                # Brief pause between batches to avoid busy-wait
                await asyncio.sleep(0.1)

        await self.stop()


# Every slash command the gateway answers. Anything else is left to north itself
# to read as an ordinary message.
_COMMAND_HANDLERS: dict[str, Callable[[TelegramGateway, _Reply, list[str]], Awaitable[None]]] = {
    "/start": TelegramGateway._command_help,
    "/help": TelegramGateway._command_help,
    "/limits": TelegramGateway._command_limits,
    "/status": TelegramGateway._command_status,
    "/cancel": TelegramGateway._command_cancel,
    "/stop": TelegramGateway._command_cancel,
    "/autonomy": TelegramGateway._command_autonomy,
}
