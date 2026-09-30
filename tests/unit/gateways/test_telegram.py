"""Unit tests for Telegram Gateway authorization and task lifecycle."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

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
async def test_telegram_gateway_tracks_and_cleans_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    from config.settings import settings

    monkeypatch.setattr(settings, "telegram_bot_token", "dummy-token-12345")
    # The gateway fails closed: with no allowlist it never starts polling.
    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", "12345")

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


@pytest.mark.asyncio
async def test_telegram_gateway_concurrent_messages_tracking(monkeypatch: pytest.MonkeyPatch) -> None:
    from config.settings import settings

    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", "12345")

    gw = TelegramGateway()
    gw._send_message = AsyncMock()  # type: ignore[method-assign]
    gw._submit_task = AsyncMock(side_effect=[{"task_id": "t1"}, {"task_id": "t2"}])  # type: ignore[method-assign]
    gw._get_task_result = AsyncMock(side_effect=["res1", "res2"])  # type: ignore[method-assign]
    gw._send_chat_action = AsyncMock()  # type: ignore[method-assign]

    msg1 = {"message_id": 101, "chat": {"id": 12345}, "from": {"id": 12345}, "text": "Task 1"}
    msg2 = {"message_id": 102, "chat": {"id": 12345}, "from": {"id": 12345}, "text": "Task 2"}

    # Process concurrently in the same chat
    await asyncio.gather(gw._process_message(msg1), gw._process_message(msg2))

    assert gw._send_message.await_count == 2
    gw._send_message.assert_any_await(12345, "res1", reply_to=101)
    gw._send_message.assert_any_await(12345, "res2", reply_to=102)
    assert len(gw._pending) == 0


@pytest.mark.asyncio
async def test_telegram_gateway_slash_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    from config.settings import settings

    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", "12345")

    gw = TelegramGateway()
    gw._send_message = AsyncMock()  # type: ignore[method-assign]
    gw._cancel_task = AsyncMock(return_value=True)  # type: ignore[method-assign]
    gw._update_settings = AsyncMock(return_value={"autonomy": "safe"})  # type: ignore[method-assign]

    # /help command
    await gw._process_message({"message_id": 1, "chat": {"id": 12345}, "text": "/help"})
    assert gw._send_message.call_args[0][0] == 12345
    assert "Available Controls" in gw._send_message.call_args[0][1]
    assert gw._send_message.call_args[1].get("reply_to") == 1

    # /cancel command when pending
    gw._pending[(12345, 99)] = {"message_id": 99, "task_id": "task_abc"}
    await gw._process_message({"message_id": 2, "chat": {"id": 12345}, "text": "/cancel"})
    gw._cancel_task.assert_awaited_with("task_abc")
    assert "cancelled" in gw._send_message.call_args[0][1].lower()

    # /autonomy auto: the old name still works, and is sent as the key the API reads.
    await gw._process_message({"message_id": 3, "chat": {"id": 12345}, "text": "/autonomy auto"})
    gw._update_settings.assert_awaited_with({"autonomy": "safe"})
    assert "`safe`" in gw._send_message.call_args[0][1]


def _autonomy_gateway(monkeypatch: pytest.MonkeyPatch) -> TelegramGateway:
    from config.approval_mode import mode_options
    from config.settings import settings

    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", "12345")
    gw = TelegramGateway()
    gw._send_message = AsyncMock()  # type: ignore[method-assign]
    gw._get_settings = AsyncMock(  # type: ignore[method-assign]
        return_value={"autonomy": "yolo", "autonomy_options": mode_options()}
    )
    gw._update_settings = AsyncMock(return_value={"autonomy": "yolo"})  # type: ignore[method-assign]
    return gw


@pytest.mark.asyncio
async def test_autonomy_lists_the_modes_the_api_serves_with_a_yolo_badge(monkeypatch: pytest.MonkeyPatch) -> None:
    gw = _autonomy_gateway(monkeypatch)

    await gw._process_message({"message_id": 1, "chat": {"id": 12345}, "text": "/autonomy"})

    reply = gw._send_message.call_args[0][1]
    for mode in ("ask", "safe", "autonomous", "yolo"):
        assert f"`{mode}`" in reply
    assert "YOLO" in reply


@pytest.mark.asyncio
async def test_autonomy_confirms_the_mode_the_api_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    gw = _autonomy_gateway(monkeypatch)

    await gw._process_message({"message_id": 1, "chat": {"id": 12345}, "text": "/autonomy yolo"})

    gw._update_settings.assert_awaited_with({"autonomy": "yolo"})
    assert "YOLO" in gw._send_message.call_args[0][1]


@pytest.mark.asyncio
async def test_an_unknown_mode_is_refused_before_anything_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    gw = _autonomy_gateway(monkeypatch)

    await gw._process_message({"message_id": 1, "chat": {"id": 12345}, "text": "/autonomy reckless"})

    gw._update_settings.assert_not_awaited()
    reply = gw._send_message.call_args[0][1]
    assert "reckless" in reply and "ask, safe, autonomous, yolo" in reply


@pytest.mark.asyncio
async def test_a_failed_update_is_not_reported_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """It used to post a key the API ignores and then say "updated" anyway."""
    gw = _autonomy_gateway(monkeypatch)
    gw._update_settings = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await gw._process_message({"message_id": 1, "chat": {"id": 12345}, "text": "/autonomy safe"})

    assert "Failed" in gw._send_message.call_args[0][1]


@pytest.mark.asyncio
async def test_telegram_gateway_callback_query_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    from config.settings import settings

    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", "12345")

    gw = TelegramGateway()
    gw._respond_approval = AsyncMock(return_value=True)  # type: ignore[method-assign]
    gw._edit_message_text = AsyncMock(return_value=True)  # type: ignore[method-assign]
    gw._answer_callback_query = AsyncMock()  # type: ignore[method-assign]

    cb = {
        "id": "cb_123",
        "from": {"id": 12345},
        "message": {
            "message_id": 55,
            "chat": {"id": 12345},
            "text": "Approval required: delete file",
        },
        "data": "approval:approved:card_xyz",
    }

    await gw._process_callback_query(cb)

    gw._respond_approval.assert_awaited_once_with("card_xyz", "approved")
    gw._edit_message_text.assert_awaited_once()
    assert "Approved" in gw._edit_message_text.call_args[0][2]
    gw._answer_callback_query.assert_awaited_once_with("cb_123", text="✅ Decision recorded: approved")


# ── Fail closed: no valid allowlist, no gateway ──────────────────────────────


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        ("", "empty"),
        ("@myname", "@myname"),
        ("12345, @myname", "@myname"),
        ("12345", ""),
        ("12345, -1001234567890", ""),  # group chats have negative ids
    ],
)
def test_allowlist_problem(raw: str, problem: str) -> None:
    found = Settings(telegram_allowed_chat_ids=raw).telegram_allowlist_problem

    assert (problem in found) if problem else found == ""


@pytest.mark.parametrize("raw", ["", "@myname"])
@pytest.mark.asyncio
async def test_gateway_does_not_start_without_a_valid_allowlist(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    from config.settings import settings

    monkeypatch.setattr(settings, "telegram_bot_token", "123:abc")
    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", raw)
    gw = TelegramGateway()
    gw.start = AsyncMock()  # type: ignore[method-assign]

    await gw.run()

    gw.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_empty_allowlist_lets_nobody_in(monkeypatch: pytest.MonkeyPatch) -> None:
    from config.settings import settings

    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", "")
    gw = TelegramGateway()
    gw._send_message = AsyncMock()  # type: ignore[method-assign]
    gw._submit_task = AsyncMock()  # type: ignore[method-assign]
    gw._answer_callback_query = AsyncMock()  # type: ignore[method-assign]
    gw._respond_approval = AsyncMock()  # type: ignore[method-assign]

    await gw._process_message({"message_id": 1, "chat": {"id": 7}, "from": {"id": 7}, "text": "hi"})
    await gw._process_callback_query(
        {"id": "cb", "from": {"id": 7}, "message": {"message_id": 2, "chat": {"id": 7}}, "data": "approval:approved:c1"}
    )

    gw._submit_task.assert_not_awaited()
    gw._respond_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_decisions_shows_who_decided_why_and_the_memory_used(monkeypatch: pytest.MonkeyPatch) -> None:
    from config.settings import settings

    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", "12345")
    gw = TelegramGateway()
    gw._send_message = AsyncMock()  # type: ignore[method-assign]
    cards = [
        {"title": "Waiting", "status": "pending"},
        {
            "title": "Deploy",
            "status": "rejected",
            "decided_by_label": "the memory decider",
            "reason": "it is Friday",
            "memory_used": [{"kind": "fact", "label": "Fact", "text": "no deploys on Fridays", "ref": ""}],
            "overruled": None,
        },
    ]
    gw._http.get = AsyncMock(return_value=MagicMock(status_code=200, json=lambda: cards))  # type: ignore[method-assign]

    await gw._process_message({"message_id": 1, "chat": {"id": 12345}, "text": "/decisions"})

    reply = gw._send_message.call_args[0][1]
    assert "Deploy" in reply and "Waiting" not in reply
    assert "by the memory decider" in reply and "it is Friday" in reply
    assert "Fact: no deploys on Fridays" in reply
