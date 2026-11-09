"""Messages for Telegram that could not be sent yet, kept on disk and tried again.

A message goes out at once when it can. When Telegram cannot be reached it is saved
here: a card was otherwise sent once and never again, so a task waited on a question
nobody saw, and a task's final answer was lost with the outage.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path

import httpx

from config.settings import settings
from gateways.telegram_api import HTTP_TIMEOUT, deliver
from utils.db import open_db_connection
from utils.time import now_epoch

logger = logging.getLogger(__name__)

FILE_NAME = "telegram_outbox.db"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    text        TEXT NOT NULL,
    reply_to    INTEGER,
    markup      TEXT,
    card_id     TEXT NOT NULL DEFAULT '',
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_epoch  REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
)
"""
_FIRST_RETRY_SECONDS = 1.0
_LONGEST_RETRY_SECONDS = 300.0
_POLL_SECONDS = 5.0


def _retry_delay(attempts: int) -> float:
    return min(_FIRST_RETRY_SECONDS * 2**attempts, _LONGEST_RETRY_SECONDS)


class TelegramOutbox:
    """Unsent messages, each retried on its own, until Telegram takes them or refuses them for good."""

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        still_wanted: Callable[[str], bool] | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._db_path = db_path or settings.north_home / FILE_NAME
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._still_wanted = still_wanted or (lambda card_id: True)
        self._http = http
        with open_db_connection(self._db_path) as conn:
            conn.execute(_SCHEMA)

    async def add(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to: int | None = None,
        reply_markup: dict | None = None,
        card_id: str = "",
    ) -> None:
        """Keep a message to try again. *card_id* drops it if the card is answered in the meantime."""
        markup = json.dumps(reply_markup) if reply_markup else None
        await asyncio.to_thread(
            self._insert, (chat_id, text, reply_to, markup, card_id, now_epoch() + _FIRST_RETRY_SECONDS)
        )

    def _insert(self, row: tuple) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                "INSERT INTO outbox (chat_id, text, reply_to, markup, card_id, next_epoch) VALUES (?, ?, ?, ?, ?, ?)",
                row,
            )

    def waiting(self) -> int:
        with open_db_connection(self._db_path) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM outbox WHERE status = 'pending'").fetchone()[0])

    def _due(self) -> list:
        with open_db_connection(self._db_path) as conn:
            return list(
                conn.execute(
                    "SELECT * FROM outbox WHERE status = 'pending' AND next_epoch <= ? ORDER BY id", (now_epoch(),)
                ).fetchall()
            )

    def _forget(self, row_id: int) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute("DELETE FROM outbox WHERE id = ?", (row_id,))

    def _try_later(self, row_id: int, attempts: int) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                "UPDATE outbox SET attempts = ?, next_epoch = ? WHERE id = ?",
                (attempts + 1, now_epoch() + _retry_delay(attempts + 1), row_id),
            )

    def _give_up(self, row_id: int) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute("UPDATE outbox SET status = 'failed' WHERE id = ?", (row_id,))

    async def drain(self) -> int:
        """Try every message that is due; returns how many went out."""
        if not settings.telegram_ready:
            return 0
        rows = await asyncio.to_thread(self._due)
        if not rows:
            return 0
        sent = 0
        client = self._http or httpx.AsyncClient(timeout=HTTP_TIMEOUT)
        try:
            for row in rows:
                if row["card_id"] and not self._still_wanted(row["card_id"]):
                    await asyncio.to_thread(self._forget, row["id"])
                    continue
                markup = json.loads(row["markup"]) if row["markup"] else None
                result = await deliver(client, row["chat_id"], row["text"], row["reply_to"], markup)
                if result.response is not None:
                    await asyncio.to_thread(self._forget, row["id"])
                    sent += 1
                elif result.worth_retrying:
                    await asyncio.to_thread(self._try_later, row["id"], row["attempts"])
                else:
                    logger.error("Telegram refused a message to %s for good; it will not be retried", row["chat_id"])
                    await asyncio.to_thread(self._give_up, row["id"])
        finally:
            if self._http is None:
                await client.aclose()
        return sent

    async def run(self) -> None:
        """Keep trying what is due."""
        while True:
            await self.drain()
            await asyncio.sleep(_POLL_SECONDS)
