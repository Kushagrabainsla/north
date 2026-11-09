"""A message Telegram could not take now is kept on disk and goes out later."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock

import httpx
import pytest

from approval.base import Notifier
from approval.models import Card, CardType
from approval.telegram import TelegramNotifier
from config.settings import settings
from gateways import telegram_outbox
from gateways.telegram import TelegramGateway, _Reply
from gateways.telegram_outbox import TelegramOutbox


@pytest.fixture(autouse=True)
def _telegram_configured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", "42")


def _client(status: int | Callable[[], int], sent: list[dict] | None = None) -> httpx.AsyncClient:
    def answer(request: httpx.Request) -> httpx.Response:
        if sent is not None:
            sent.append({"body": request.content})
        code = status() if callable(status) else status
        return httpx.Response(code, json={"ok": code == 200, "result": {"message_id": 1}})

    return httpx.AsyncClient(transport=httpx.MockTransport(answer))


def _make_due(monkeypatch) -> None:
    """Move the clock forward, past any wait."""
    from utils.time import now_epoch

    now = now_epoch()
    monkeypatch.setattr(telegram_outbox, "now_epoch", lambda: now + 1000)


async def test_a_kept_message_goes_out_and_is_forgotten(tmp_path, monkeypatch) -> None:
    sent: list[dict] = []
    outbox = TelegramOutbox(tmp_path / "o.db", http=_client(200, sent))
    await outbox.add(42, "hello", reply_to=7)
    _make_due(monkeypatch)

    assert await outbox.drain() == 1
    assert b'"reply_to_message_id":7' in sent[0]["body"]
    assert outbox.waiting() == 0


async def test_a_message_outlives_a_restart(tmp_path, monkeypatch) -> None:
    await TelegramOutbox(tmp_path / "o.db", http=_client(500)).add(42, "hello")
    _make_due(monkeypatch)

    after_restart = TelegramOutbox(tmp_path / "o.db", http=_client(200))

    assert after_restart.waiting() == 1
    assert await after_restart.drain() == 1


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_an_outage_is_tried_again_later(tmp_path, monkeypatch, status: int) -> None:
    sent: list[dict] = []
    outbox = TelegramOutbox(tmp_path / "o.db", http=_client(status, sent))
    await outbox.add(42, "hello")
    _make_due(monkeypatch)

    assert await outbox.drain() == 0
    assert outbox.waiting() == 1
    tries = len(sent)
    await outbox.drain()
    assert len(sent) == tries, "not due again yet"


async def test_a_network_failure_is_tried_again(tmp_path, monkeypatch) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    outbox = TelegramOutbox(tmp_path / "o.db", http=httpx.AsyncClient(transport=httpx.MockTransport(down)))
    await outbox.add(42, "hello")
    _make_due(monkeypatch)

    await outbox.drain()

    assert outbox.waiting() == 1


async def test_a_message_telegram_refuses_for_good_stops_being_tried(tmp_path, monkeypatch) -> None:
    sent: list[dict] = []
    outbox = TelegramOutbox(tmp_path / "o.db", http=_client(403, sent))
    await outbox.add(42, "hello")
    _make_due(monkeypatch)

    await outbox.drain()
    await outbox.drain()

    assert len(sent) == 1
    assert outbox.waiting() == 0


async def test_a_card_answered_meanwhile_is_not_sent(tmp_path, monkeypatch) -> None:
    sent: list[dict] = []
    outbox = TelegramOutbox(tmp_path / "o.db", http=_client(200, sent), still_wanted=lambda card_id: False)
    await outbox.add(42, "approve?", card_id="c1")
    _make_due(monkeypatch)

    assert await outbox.drain() == 0
    assert sent == []
    assert outbox.waiting() == 0


async def test_waits_grow_and_have_a_limit() -> None:
    assert telegram_outbox._retry_delay(0) == 1.0
    assert telegram_outbox._retry_delay(3) == 8.0
    assert telegram_outbox._retry_delay(40) == 300.0


# ---- who keeps a message ----


class _Silent(Notifier):
    async def notify(self, card: Card) -> None:
        pass


def _card(kind: CardType = CardType.APPROVAL) -> Card:
    return Card.new(type=kind, agent="coder", title="t", message="m")


async def test_a_card_that_could_not_be_sent_is_kept_with_its_buttons(tmp_path) -> None:
    outbox = TelegramOutbox(tmp_path / "o.db")
    card = _card()

    await TelegramNotifier(_Silent(), http=_client(500), outbox=outbox).notify(card)

    (row,) = outbox._due() or _all(outbox)
    assert row["card_id"] == card.id
    assert card.id in row["markup"]


async def test_a_note_that_nothing_answers_is_never_dropped_as_answered(tmp_path) -> None:
    outbox = TelegramOutbox(tmp_path / "o.db")

    await TelegramNotifier(_Silent(), http=_client(500), outbox=outbox).notify(_card(CardType.INFORMATION))

    (row,) = _all(outbox)
    assert row["card_id"] == ""


async def test_a_card_that_was_sent_is_not_kept(tmp_path) -> None:
    outbox = TelegramOutbox(tmp_path / "o.db")

    await TelegramNotifier(_Silent(), http=_client(200), outbox=outbox).notify(_card())

    assert outbox.waiting() == 0


async def test_a_tasks_answer_is_kept_when_it_cannot_be_sent() -> None:
    gateway = TelegramGateway()
    gateway._await_result_while_typing = AsyncMock(return_value="the answer")  # type: ignore[method-assign]
    gateway._send_message = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await gateway._answer(_Reply(chat_id=42, message_id=7), "do it", "t1")

    (row,) = _all(gateway._outbox)
    assert (row["chat_id"], row["text"], row["reply_to"]) == (42, "the answer", 7)


def _all(outbox: TelegramOutbox) -> list:
    from utils.db import open_db_connection

    with open_db_connection(outbox._db_path) as conn:
        return list(conn.execute("SELECT * FROM outbox").fetchall())
