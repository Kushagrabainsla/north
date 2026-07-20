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
        self._pending: dict[int, dict] = {}  # chat_id -> {message_id, text, task_id}
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
        await self._http.aclose()
        logger.info("Telegram gateway stopped")

    async def _get_updates(self) -> list[dict]:
        """Long-poll Telegram for new messages."""
        try:
            resp = await self._http.post(
                _bot_url("getUpdates"),
                json={
                    "offset": self._offset,
                    "timeout": 25,  # long-poll (seconds)
                    "allowed_updates": ["message"],
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

    async def _send_message(self, chat_id: int, text: str, reply_to: int | None = None) -> None:
        """Send a message to a Telegram chat."""
        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        try:
            resp = await self._http.post(_bot_url("sendMessage"), json=payload)
            resp.raise_for_status()
        except httpx.RequestError as exc:
            logger.error("Failed to send Telegram message to %s: %s", chat_id, exc)

    async def _send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        """Show a typing indicator in the chat."""
        with contextlib.suppress(httpx.RequestError):
            await self._http.post(_bot_url("sendChatAction"), json={"chat_id": chat_id, "action": action})

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

    async def _get_task_result(self, task_id: str) -> str | None:
        """Poll the ledger for the completed agent's output."""
        url = f"{self._orchestrator_base}/orchestrator/ledger"
        params = {"task_id": task_id, "limit": 50}
        for _ in range(30):
            try:
                resp = await self._http.get(url, params=params, headers=_headers())
                if resp.status_code == 200:
                    entries = resp.json()
                    # Scan for the agent's actual response (agent_completed
                    # carries the output; task_completed, classified_as_*,
                    # skill_selected etc are just pipeline bookkeeping).
                    for entry in entries:  # most-recent-first
                        action = entry.get("action", "")
                        if action in ("agent_completed",) and entry.get("output"):
                            return entry["output"]
                    # If the terminal entry says failed/cancelled, report that.
                    for entry in entries:
                        status = (entry.get("status") or "").lower()
                        if status in ("failed", "cancelled") and entry.get("output"):
                            return f"Task {status}: {entry['output']}"
                elif resp.status_code == 404:
                    return "Task not found."
            except httpx.RequestError:
                pass
            await asyncio.sleep(_TASK_POLL_INTERVAL)
        return "Response timed out — check north for details."

    async def _process_message(self, msg: dict) -> None:
        """Process one incoming Telegram message."""
        chat_id = msg["chat"]["id"]
        message_id = msg["message_id"]
        text = msg.get("text", "").strip()

        if not text:
            return

        # Ignore commands we don't handle
        if text.startswith("/"):
            if text == "/start":
                await self._send_message(
                    chat_id,
                    "👋 Hello! I'm **north** — your personal life operating system.\n\n"
                    "Just send me a message and I'll process it through my agents.\n"
                    "Examples:\n"
                    "  • \"What's on my calendar today?\"\n"
                    "  • \"Check my budget for groceries\"\n"
                    "  • \"Remind me about the dentist appointment\"",
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
        self._pending[chat_id] = {
            "message_id": message_id,
            "text": text,
            "task_id": task_id,
        }

        # Poll for result
        output = await self._get_task_result(task_id)

        # Send result back
        if output:
            # Truncate long messages to avoid Telegram limits (4096 chars)
            if len(output) > 3900:
                output = output[:3900] + "\n\n[truncated — see north for full response]"
            await self._send_message(chat_id, output, reply_to=message_id)
        else:
            msg = (
                f"✅ Task submitted (ID: `{task_id}`)."
                " Check north for results."
            )
            await self._send_message(chat_id, msg, reply_to=message_id)

        # Remove from pending
        self._pending.pop(chat_id, None)

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
                        # Process in its own task so we don't block the poller
                        asyncio.create_task(self._process_message(msg))
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
