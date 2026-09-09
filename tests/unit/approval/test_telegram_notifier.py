"""north delivering a card nobody asked for.

Issue #2: north could only ever reply. A task started by cron finished, emitted
a card, and the card went to a terminal nobody was watching - and if it needed
an approval, that expired in five minutes and the action was silently denied.

The test that matters here is the last one: a card for a task the Telegram
gateway never saw. Anything keyed off an inbound message passes today and proves
nothing, because being tracked from an inbound message is exactly what a
cron-launched task is not.
"""

from __future__ import annotations

import httpx
import pytest

from approval.base import Notifier
from approval.models import Card, CardField, CardType
from approval.telegram import TelegramNotifier
from approval.tui import TUIAwareNotifier
from gateways.telegram_api import parse_approval_callback


class _Recorder(Notifier):
    def __init__(self) -> None:
        self.cards: list[Card] = []

    async def notify(self, card: Card) -> None:
        self.cards.append(card)


class _FakeHttp:
    """Stands in for httpx, recording what would go to Telegram."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict] = []
        self.fail = fail

    async def post(self, url: str, json: dict):
        self.sent.append(json)
        if self.fail:
            raise httpx.RequestError("network down")
        return _Resp()


class _Resp:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"ok": True, "result": {"message_id": 1}}


@pytest.fixture(autouse=True)
def _telegram_configured(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", "111,222")
    yield


def _card(**kw) -> Card:
    defaults = {
        "type": CardType.APPROVAL,
        "agent": "coder",
        "title": "Run migration",
        "message": "This drops a column.",
    }
    defaults.update(kw)
    return Card.new(**defaults)


# ── Delivery ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_card_reaches_every_allowed_chat() -> None:
    http = _FakeHttp()
    fallback = _Recorder()

    await TelegramNotifier(fallback=fallback, http=http).notify(_card())

    assert [m["chat_id"] for m in http.sent] == [111, 222]
    assert not fallback.cards, "a delivered card must not also hit the terminal"


@pytest.mark.asyncio
async def test_an_approval_card_carries_answerable_buttons() -> None:
    """Delivering a question you cannot answer is not delivery."""
    http = _FakeHttp()
    card = _card()

    await TelegramNotifier(fallback=_Recorder(), http=http).notify(card)

    buttons = http.sent[0]["reply_markup"]["inline_keyboard"][0]
    decisions = [parse_approval_callback(b["callback_data"]) for b in buttons]
    assert decisions == [("approved", card.id), ("rejected", card.id)]


@pytest.mark.asyncio
async def test_an_information_card_carries_no_buttons() -> None:
    http = _FakeHttp()
    await TelegramNotifier(fallback=_Recorder(), http=http).notify(_card(type=CardType.INFORMATION))
    assert "reply_markup" not in http.sent[0]


@pytest.mark.asyncio
async def test_prepared_work_shows_its_values_and_says_nothing_is_blocked() -> None:
    """ "north has something for you" is not reviewable; the fields are the point."""
    http = _FakeHttp()
    card = _card(
        title="Application ready",
        fields=[CardField(name="company", value="Acme"), CardField(name="role", value="Staff Engineer")],
        context="We are hiring a Staff Engineer to...",
        blocking=False,
        source="job_applications",
    )

    await TelegramNotifier(fallback=_Recorder(), http=http).notify(card)

    text = http.sent[0]["text"]
    assert "Acme" in text and "Staff Engineer" in text
    assert "We are hiring" in text, "the source material must travel with the work"
    assert "nothing is blocked" in text


# ── Degrading ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_unconfigured_install_behaves_exactly_as_before(monkeypatch) -> None:
    from config.settings import settings

    monkeypatch.setattr(settings, "telegram_bot_token", "")
    fallback = _Recorder()

    await TelegramNotifier(fallback=fallback, http=_FakeHttp()).notify(_card())

    assert len(fallback.cards) == 1


@pytest.mark.asyncio
async def test_a_card_that_reached_no_chat_falls_back() -> None:
    """Telegram configured and still failing must not silently swallow the card."""
    fallback = _Recorder()

    await TelegramNotifier(fallback=fallback, http=_FakeHttp(fail=True)).notify(_card())

    assert len(fallback.cards) == 1


# ── The bug this issue was actually about ────────────────────────────────────


class _ConnectedTUI:
    tui_connected = True


@pytest.mark.asyncio
async def test_an_open_tui_no_longer_swallows_a_card_from_an_unattended_task() -> None:
    """A cron job at 03:00 is not a task anyone is watching.

    TUIAwareNotifier stayed silent whenever a TUI was connected, on the reasoning
    that SSE carries the card inline. That holds for the task you are looking at
    and fails for one raised by a schedule while a TUI happens to be left open.
    """
    fallback = _Recorder()
    notifier = TUIAwareNotifier(stream_manager=_ConnectedTUI(), fallback=fallback)

    await notifier.notify(_card(source="nightly_backup"))
    await notifier.notify(_card(blocking=False))

    assert len(fallback.cards) == 2, "a card the TUI is not showing must still be delivered"


@pytest.mark.asyncio
async def test_an_open_tui_still_suppresses_the_card_it_is_showing() -> None:
    """The original behaviour has to survive: no double alert for a live approval."""
    fallback = _Recorder()
    notifier = TUIAwareNotifier(stream_manager=_ConnectedTUI(), fallback=fallback)

    await notifier.notify(_card(task_id="t1"))

    assert not fallback.cards
