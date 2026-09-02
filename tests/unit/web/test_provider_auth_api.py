from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException

from inference.auth import AuthStatus
from orchestrator.api_context import ApiServices, attach, bind_services, services_of
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
def web_app(monkeypatch):
    """One app per test, wired with its own runtime state.

    In-flight logins now live on the app rather than at module scope, so tests no
    longer clear shared state before and after - each gets a clean app.
    """
    _FakeCodexCredentials.connected = False
    monkeypatch.setattr(web_api, "CodexCredentialProvider", _FakeCodexCredentials)
    monkeypatch.setattr(web_api, "_refresh_inference_runtime", AsyncMock())
    app = FastAPI()
    attach(app, ApiServices(web_runtime=web_api.WebRuntime()))
    with bind_services(services_of(app)):
        yield app


def _sessions(app: FastAPI) -> dict:
    return services_of(app).web_runtime.auth_sessions


def _request(app: FastAPI) -> SimpleNamespace:
    """A stand-in for the Request the route uses only to reach `.app`."""
    return SimpleNamespace(app=app)


@pytest.mark.asyncio
async def test_dashboard_can_complete_codex_login_and_logout(web_app: FastAPI) -> None:
    started = await web_api.start_provider_auth("openai-codex", _request(web_app))
    assert started["state"] == "pending"
    assert started["authorization_url"].startswith("https://auth.openai.test/")
    assert started["configured"] is False

    session = _sessions(web_app)["openai_codex"]
    assert session.task is not None
    await session.task

    connected = await web_api.provider_auth_status("openai_codex")
    assert connected["state"] == "connected"
    assert connected["configured"] is True
    assert connected["account_hint"] == "…123456"

    disconnected = await web_api.logout_provider("openai_codex", _request(web_app))
    assert disconnected["state"] == "disconnected"
    assert disconnected["configured"] is False
    assert web_api._refresh_inference_runtime.await_count == 2


@pytest.mark.asyncio
async def test_dashboard_reuses_active_codex_login(web_app: FastAPI) -> None:
    first = await web_api.start_provider_auth("openai_codex", _request(web_app))
    second = await web_api.start_provider_auth("openai_codex", _request(web_app))

    assert first["authorization_url"] == second["authorization_url"]
    assert len(_sessions(web_app)) == 1
    task = _sessions(web_app)["openai_codex"].task
    assert task is not None
    await task


@pytest.mark.asyncio
async def test_browser_login_endpoint_rejects_api_key_provider(web_app: FastAPI) -> None:
    with pytest.raises(HTTPException) as exc:
        await web_api.start_provider_auth("groq", _request(web_app))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_logins_do_not_leak_between_apps(web_app: FastAPI) -> None:
    """Two apps keep separate in-flight logins - the point of moving off globals."""
    await web_api.start_provider_auth("openai_codex", _request(web_app))
    other = FastAPI()
    attach(other, ApiServices(web_runtime=web_api.WebRuntime()))

    assert len(_sessions(web_app)) == 1
    assert len(_sessions(other)) == 0

    task = _sessions(web_app)["openai_codex"].task
    assert task is not None
    await task
