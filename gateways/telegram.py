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

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot"
_POLL_INTERVAL = 2.0  # seconds between long-poll requests
_TASK_POLL_INTERVAL = 1.0  # seconds between checking task status
_TASK_POLL_MAX_ATTEMPTS = 90  # 90 × 1s = 90s max wait for task completion
_MAX_RETRIES = 3
_HTTP_TIMEOUT = 30.0


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
        url = f"{self._orchestrator_base}/orchestrator/ledger"
        params = {"task_id": task_id, "limit": 50}
        prompted_cards: set[str] = set()

        for i in range(_TASK_POLL_MAX_ATTEMPTS):
            try:
                resp = await self._http.get(url, params=params, headers=_headers())
                if resp.status_code == 200:
                    entries = resp.json()

                    # Check for interactive approval cards
                    if chat_id is not None:
                        for entry in entries:
                            action = entry.get("action", "")
                            if action == "approval_required":
                                card_id = entry.get("card_id") or ""
                                if card_id and card_id not in prompted_cards:
                                    prompted_cards.add(card_id)
                                    msg_text = (
                                        f"⚠️ **Approval Required**\n\n"
                                        f"Task `{task_id}` requires your confirmation to proceed:\n"
                                        f"_{entry.get('message', 'Confirm action')}_"
                                    )
                                    markup = {
                                        "inline_keyboard": [
                                            [
                                                {"text": "✅ Approve", "callback_data": f"approval:approved:{card_id}"},
                                                {"text": "❌ Reject", "callback_data": f"approval:rejected:{card_id}"},
                                            ]
                                        ]
                                    }
                                    await self._send_message(chat_id, msg_text, reply_markup=markup)

                    # Scan for the final synthesized or completed task output first
                    for entry in entries:  # most-recent-first
                        action = entry.get("action", "")
                        is_completed_action = action in (
                            "task_synthesis",
                            "task_completed",
                            "task_completed_with_failures",
                        )
                        if is_completed_action and entry.get("output"):
                            logger.info("Task %s found %s output at poll %d", task_id, action, i)
                            return entry["output"]

                    # If the terminal entry says failed/cancelled, report that.
                    for entry in entries:
                        status = (entry.get("status") or "").lower()
                        if status in ("failed", "cancelled") and entry.get("output"):
                            logger.warning("Task %s terminal status=%s at poll %d", task_id, status, i)
                            return f"Task {status}: {entry['output']}"

                    # Fallback to single agent completion
                    for entry in entries:
                        action = entry.get("action", "")
                        if action in ("agent_completed",) and entry.get("output"):
                            logger.info("Task %s found agent_completed output at poll %d", task_id, i)
                            return entry["output"]
                elif resp.status_code == 404:
                    return "Task not found."
            except httpx.RequestError:
                pass
            await asyncio.sleep(_TASK_POLL_INTERVAL)
        logger.error(
            "Task %s timed out after %d polls (%ds)",
            task_id,
            _TASK_POLL_MAX_ATTEMPTS,
            _TASK_POLL_MAX_ATTEMPTS,
        )
        return "Response timed out — check north for details."

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
        chat_id = msg["chat"]["id"]
        from_id = msg.get("from", {}).get("id")
        allowed = settings.parsed_telegram_allowed_chat_ids
        if allowed and chat_id not in allowed and (from_id is None or from_id not in allowed):
            logger.warning("Unauthorized Telegram message from chat_id=%s from_id=%s", chat_id, from_id)
            await self._send_message(
                chat_id,
                "⛔ Unauthorized: this Telegram account/chat is not on the allowed list for this North instance.",
                reply_to=msg.get("message_id"),
            )
            return

        message_id = msg["message_id"]
        text = msg.get("text", "").strip()
        voice = msg.get("voice")

        # Handle voice messages: download → transcribe → submit as text task
        if voice:
            await self._send_chat_action(chat_id, "record_voice")
            file_id = voice.get("file_id")
            if not file_id:
                await self._send_message(chat_id, "❌ Could not read voice message.", reply_to=message_id)
                return
            audio_bytes = await self._download_file(file_id)
            if not audio_bytes:
                await self._send_message(chat_id, "❌ Failed to download voice message.", reply_to=message_id)
                return
            await self._send_chat_action(chat_id, "typing")
            transcribed = await self._transcribe_audio(audio_bytes)
            if not transcribed:
                await self._send_message(chat_id, "❌ Could not transcribe voice message.", reply_to=message_id)
                return
            # Submit transcribed text as a normal task
            text = transcribed

        if not text:
            return

        # Slash commands
        if text.startswith("/"):
            cmd = text.split()[0]
            args = text.split()[1:]

            if cmd in ("/start", "/help"):
                await self._send_message(
                    chat_id,
                    "👋 **Welcome to North** — Your Autonomous Assistant\n\n"
                    "Send any message or voice note to execute tasks.\n\n"
                    "**Available Controls:**\n"
                    "  • `/status` — View active tasks & orchestrator status\n"
                    "  • `/cancel` — Cancel the currently running task\n"
                    "  • `/autonomy` — View or set approval mode (`/autonomy interactive|auto|autonomous`)\n"
                    "  • `/limits` — Show provider/model rate-limit & cooldown status\n"
                    "  • `/help` — Show this command reference",
                    reply_to=message_id,
                )
            elif cmd == "/limits":
                await self._send_limits(chat_id, reply_to=message_id)
            elif cmd == "/status":
                url = f"{self._orchestrator_base}/orchestrator/tasks"
                try:
                    resp = await self._http.get(url, headers=_headers())
                    if resp.status_code == 200:
                        tasks = resp.json()
                        if not tasks:
                            await self._send_message(
                                chat_id, "🟢 **Status:** Idle — No active tasks running.", reply_to=message_id
                            )
                        else:
                            lines = [f"🔄 **Active Tasks ({len(tasks)}):**"]
                            for t in tasks[:5]:
                                lines.append(f"  • `{t.get('task_id')}`: {t.get('status')} ({t.get('agent', 'orch')})")
                            await self._send_message(chat_id, "\n".join(lines), reply_to=message_id)
                    else:
                        await self._send_message(
                            chat_id, "⚠️ Could not retrieve tasks from orchestrator.", reply_to=message_id
                        )
                except httpx.RequestError as exc:
                    await self._send_message(chat_id, f"❌ Connection error: {exc}", reply_to=message_id)
            elif cmd in ("/cancel", "/stop"):
                # Find pending task for this chat or target arg
                target_task = args[0] if args else None
                if not target_task:
                    for (c_id, _), item in self._pending.items():
                        if c_id == chat_id:
                            target_task = item.get("task_id")
                            break
                if target_task:
                    success = await self._cancel_task(target_task)
                    if success:
                        await self._send_message(chat_id, f"🛑 Task `{target_task}` cancelled.", reply_to=message_id)
                    else:
                        await self._send_message(
                            chat_id, f"❌ Failed to cancel task `{target_task}`.", reply_to=message_id
                        )
                else:
                    await self._send_message(chat_id, "ℹ️ No running tasks found to cancel.", reply_to=message_id)
            elif cmd == "/autonomy":
                if args:
                    new_mode = args[0].lower()
                    if new_mode in ("interactive", "auto", "autonomous"):
                        updated = await self._update_settings({"approval_mode": new_mode})
                        if updated:
                            await self._send_message(
                                chat_id, f"✅ Approval mode updated to: `{new_mode}`", reply_to=message_id
                            )
                        else:
                            await self._send_message(chat_id, "❌ Failed to update approval mode.", reply_to=message_id)
                    else:
                        await self._send_message(
                            chat_id,
                            "⚠️ Invalid mode. Choose: `interactive`, `auto`, or `autonomous`.",
                            reply_to=message_id,
                        )
                else:
                    current = await self._get_settings()
                    mode = current.get("approval_mode", "interactive") if current else settings.approval_mode
                    await self._send_message(
                        chat_id,
                        f"⚙️ Current approval mode: `{mode}`\nUse `/autonomy <mode>` to change.",
                        reply_to=message_id,
                    )
            return

        # Show typing indicator
        await self._send_chat_action(chat_id)

        # Submit to north
        result = await self._submit_task(text)
        if result is None:
            await self._send_message(chat_id, "❌ Failed to connect to north.", reply_to=message_id)
            return

        task_id = result.get("task_id", "")
        if not task_id:
            await self._send_message(chat_id, "❌ North did not return a task ID.", reply_to=message_id)
            return

        # Store in pending map so we can match results
        pending_key = (chat_id, message_id)
        self._pending[pending_key] = {
            "message_id": message_id,
            "text": text,
            "task_id": task_id,
        }

        # Poll for result with continuous typing indicator
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(self._typing_keepalive(chat_id, stop_typing))
        try:
            output = await self._get_task_result(task_id, chat_id=chat_id)
        finally:
            stop_typing.set()
            typing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await typing_task

        # Send result back
        if output:
            # Truncate long messages to avoid Telegram limits (4096 chars)
            if len(output) > 3900:
                output = output[:3900] + "\n\n[truncated — see north for full response]"
            await self._send_message(chat_id, output, reply_to=message_id)
        else:
            msg = f"✅ Task submitted (ID: `{task_id}`). Check north for results."
            await self._send_message(chat_id, msg, reply_to=message_id)

        # Remove from pending
        self._pending.pop(pending_key, None)

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
