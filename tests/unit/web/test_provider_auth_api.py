from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from inference.auth import AuthStatus
from web import api as web_api


class _FakeCodexCredentials:
    connected = False

    def __init__(self, *, authorization_callback=None) -> None:
        self.authorization_callback = authorization_callback

    def status(self) -> AuthStatus:
        return AuthStatus(
            configured=self.connected,
            provider_id="openai_codex",
            account_id="account-123456" if self.connected else None,
            needs_login=not self.connected,
            detail="Logged in" if self.connected else "Not logged in",
        )

    async def login(self, *, open_browser: bool = True) -> AuthStatus:
        assert open_browser is False
        if self.authorization_callback:
            self.authorization_callback("https://auth.openai.test/authorize?state=safe")
        await asyncio.sleep(0.01)
        type(self).connected = True
        return self.status()

    async def logout(self) -> None:
        type(self).connected = False


@pytest.fixture(autouse=True)
def _reset_auth_state(monkeypatch):
    web_api._provider_auth_sessions.clear()
    _FakeCodexCredentials.connected = False
    monkeypatch.setattr(web_api, "CodexCredentialProvider", _FakeCodexCredentials)
    monkeypatch.setattr(web_api, "_refresh_inference_runtime", AsyncMock())
    yield
    web_api._provider_auth_sessions.clear()


@pytest.mark.asyncio
async def test_dashboard_can_complete_codex_login_and_logout() -> None:
    started = await web_api.start_provider_auth("openai-codex")
    assert started["state"] == "pending"
    assert started["authorization_url"].startswith("https://auth.openai.test/")
    assert started["configured"] is False

    session = web_api._provider_auth_sessions["openai_codex"]
    assert session.task is not None
    await session.task

    connected = await web_api.provider_auth_status("openai_codex")
    assert connected["state"] == "connected"
    assert connected["configured"] is True
    assert connected["account_hint"] == "…123456"

    disconnected = await web_api.logout_provider("openai_codex")
    assert disconnected["state"] == "disconnected"
    assert disconnected["configured"] is False
    assert web_api._refresh_inference_runtime.await_count == 2


@pytest.mark.asyncio
async def test_dashboard_reuses_active_codex_login() -> None:
    first = await web_api.start_provider_auth("openai_codex")
    second = await web_api.start_provider_auth("openai_codex")

    assert first["authorization_url"] == second["authorization_url"]
    assert len(web_api._provider_auth_sessions) == 1
    task = web_api._provider_auth_sessions["openai_codex"].task
    assert task is not None
    await task


@pytest.mark.asyncio
async def test_browser_login_endpoint_rejects_api_key_provider() -> None:
    with pytest.raises(HTTPException) as exc:
        await web_api.start_provider_auth("groq")
    assert exc.value.status_code == 400

