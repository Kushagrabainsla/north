"""Unit tests for Telegram Gateway authorization and task lifecycle."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from config.settings import Settings
from gateways.telegram import TelegramGateway


def test_parsed_telegram_allowed_chat_ids() -> None:
    s = Settings(telegram_allowed_chat_ids="12345, 67890, invalid, 11111")
    allowed = s.parsed_telegram_allowed_chat_ids
    assert allowed == frozenset({12345, 67890, 11111})

    empty = Settings(telegram_allowed_chat_ids="")
    assert empty.parsed_telegram_allowed_chat_ids == frozenset()


@pytest.mark.asyncio
async def test_telegram_gateway_rejects_unauthorized_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    from config.settings import settings

    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", "999999")

    gw = TelegramGateway()
    gw._send_message = AsyncMock()  # type: ignore[method-assign]
    gw._submit_task = AsyncMock()  # type: ignore[method-assign]

    unauthorized_msg = {
        "message_id": 1,
        "chat": {"id": 12345},
        "from": {"id": 12345},
        "text": "Hello world",
    }

    await gw._process_message(unauthorized_msg)

    # Should send unauthorized response and NOT submit task
    gw._send_message.assert_awaited_once()
    call_args = gw._send_message.call_args[0]
    assert call_args[0] == 12345
    assert "Unauthorized" in call_args[1]
    gw._submit_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_gateway_allows_authorized_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    from config.settings import settings

    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", "12345,67890")

    gw = TelegramGateway()
    gw._send_message = AsyncMock()  # type: ignore[method-assign]
    gw._submit_task = AsyncMock(return_value={"task_id": "t123"})  # type: ignore[method-assign]
    gw._get_task_result = AsyncMock(return_value="Task result output")  # type: ignore[method-assign]
    gw._send_chat_action = AsyncMock()  # type: ignore[method-assign]

    authorized_msg = {
        "message_id": 10,
        "chat": {"id": 12345},
        "from": {"id": 12345},
        "text": "Check weather",
    }

    await gw._process_message(authorized_msg)

    gw._submit_task.assert_awaited_once_with("Check weather")
    gw._send_message.assert_awaited_once_with(12345, "Task result output", reply_to=10)


@pytest.mark.asyncio
async def test_telegram_gateway_tracks_and_cleans_tasks() -> None:
    gw = TelegramGateway()
    gw._http = AsyncMock()  # type: ignore[method-assign]
    gw._running = True

    processed = asyncio.Event()

    async def mock_process(msg):
        await asyncio.sleep(0.01)
        processed.set()

    gw._process_message = mock_process  # type: ignore[method-assign]
    gw._get_updates = AsyncMock(return_value=[{"update_id": 1, "message": {"text": "hi"}}])  # type: ignore[method-assign]

    run_task = asyncio.create_task(gw.run())
    await asyncio.wait_for(processed.wait(), timeout=3.0)
    assert processed.is_set()

    await gw.stop()
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert len(gw._tasks) == 0
