"""Telegram is set up on the dashboard: checked with Telegram, saved to .env, live without a restart."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import pytest
from fastapi import HTTPException

from config.settings import settings
from orchestrator.api_context import ApiServices, bind_services
from web import api

TOKEN = "123456789:AAbbCCddEEffGGhhIIjjKKllMMnnOOppQQr"


async def _bot(token: str) -> str:
    return "north_test_bot"


class _Telegram:
    """Stands in for Telegram's getMe; a test swaps `answer` to make it refuse or be unreachable."""

    def __init__(self) -> None:
        self.answer: Callable[[str], Awaitable[str]] = _bot

    async def __call__(self, token: str) -> str:
        return await self.answer(token)


@pytest.fixture
def telegram() -> _Telegram:
    return _Telegram()


@pytest.fixture
def home(tmp_path, monkeypatch, telegram):
    # Saving sets these in the process environment; set through monkeypatch first
    # so they are put back afterwards, and read .env from here, not ~/.north.
    for key in ("NORTH_TELEGRAM_BOT_TOKEN", "NORTH_TELEGRAM_ALLOWED_CHAT_IDS"):
        monkeypatch.setenv(key, "")
    monkeypatch.setitem(settings.model_config, "env_file", str(tmp_path / ".env"))
    with bind_services(ApiServices(north_home=tmp_path, telegram_bot_name=telegram)):
        yield tmp_path


@pytest.mark.asyncio
async def test_saving_checks_the_token_with_telegram_and_the_gateway_sees_it_live(home) -> None:
    saved = await api.update_telegram(api.TelegramUpdate(bot_token=TOKEN, allowed_chat_ids=" 12345 , 678 "))

    assert saved["bot"] == "north_test_bot" and saved["configured"]
    assert TOKEN not in str(saved) and saved["token_hint"] == f"{TOKEN[:4]}…{TOKEN[-4:]}"
    assert settings.telegram_ready and settings.telegram_allowed_chat_ids == "12345,678"
    env = (home / ".env").read_text(encoding="utf-8")
    assert f"NORTH_TELEGRAM_BOT_TOKEN={TOKEN}" in env and "NORTH_TELEGRAM_ALLOWED_CHAT_IDS=12345,678" in env
    assert (home / ".env").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_the_allowlist_changes_without_pasting_the_token_again(home) -> None:
    await api.update_telegram(api.TelegramUpdate(bot_token=TOKEN, allowed_chat_ids="12345"))

    await api.update_telegram(api.TelegramUpdate(allowed_chat_ids="999"))

    assert settings.telegram_bot_token == TOKEN and settings.telegram_allowed_chat_ids == "999"


@pytest.mark.parametrize(
    ("token", "ids", "detail"),
    [
        ("not-a-token", "12345", "@BotFather"),
        (TOKEN, "@myname", "numeric"),
        (TOKEN, "", "numeric"),
    ],
)
@pytest.mark.asyncio
async def test_a_bad_token_or_allowlist_is_refused_before_anything_is_saved(home, token, ids, detail) -> None:
    with pytest.raises(HTTPException) as error:
        await api.update_telegram(api.TelegramUpdate(bot_token=token, allowed_chat_ids=ids))

    assert error.value.status_code == 400 and detail in error.value.detail
    assert not (home / ".env").exists()


@pytest.mark.asyncio
async def test_a_token_telegram_rejects_is_not_saved(home, telegram) -> None:
    async def rejected(token: str) -> str:
        raise ValueError("Unauthorized")

    telegram.answer = rejected

    with pytest.raises(HTTPException) as error:
        await api.update_telegram(api.TelegramUpdate(bot_token=TOKEN, allowed_chat_ids="12345"))

    assert error.value.status_code == 400 and "Unauthorized" in error.value.detail
    assert not (home / ".env").exists()


@pytest.mark.asyncio
async def test_an_unreachable_telegram_never_echoes_the_token(home, telegram) -> None:
    async def offline(token: str) -> str:
        raise httpx.ConnectError(f"https://api.telegram.org/bot{token}/getMe")

    telegram.answer = offline

    with pytest.raises(HTTPException) as error:
        await api.update_telegram(api.TelegramUpdate(bot_token=TOKEN, allowed_chat_ids="12345"))

    assert error.value.status_code == 502 and TOKEN not in error.value.detail


@pytest.mark.asyncio
async def test_disconnect_forgets_the_token_and_keeps_who_may_use_it(home) -> None:
    await api.update_telegram(api.TelegramUpdate(bot_token=TOKEN, allowed_chat_ids="12345"))

    status = await api.disconnect_telegram()

    assert not status["configured"] and status["token_hint"] == "" and status["allowed_chat_ids"] == "12345"
    assert not settings.telegram_ready
    assert "NORTH_TELEGRAM_BOT_TOKEN=\n" in (home / ".env").read_text(encoding="utf-8")
