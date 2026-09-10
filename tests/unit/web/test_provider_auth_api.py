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


class _RecordingFactory:
    """A server-owned factory that records how the web layer builds providers.

    Distinct from the module-level ``CodexCredentialProvider`` name so a test can
    prove the endpoints went through the *injected* factory, not the class.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *, authorization_callback=None):
        self.calls.append({"authorization_callback": authorization_callback})
        return _FakeCodexCredentials(authorization_callback=authorization_callback)


@pytest.fixture
def injected_app(monkeypatch):
    """An app wired with an explicit ``codex_credentials_factory``.

    ``CodexCredentialProvider`` is replaced with a bomb so any direct
    construction inside a route body fails the test loudly.
    """
    _FakeCodexCredentials.connected = False

    def _bomb(*args, **kwargs):  # pragma: no cover - only runs on regression
        raise AssertionError("web layer constructed CodexCredentialProvider directly")

    monkeypatch.setattr(web_api, "CodexCredentialProvider", _bomb)
    monkeypatch.setattr(web_api, "_refresh_inference_runtime", AsyncMock())
    factory = _RecordingFactory()
    app = FastAPI()
    attach(
        app,
        ApiServices(web_runtime=web_api.WebRuntime(), codex_credentials_factory=factory),
    )
    with bind_services(services_of(app)):
        yield app, factory


@pytest.mark.asyncio
async def test_endpoints_use_injected_factory_not_direct_construction(injected_app) -> None:
    """Status, login and logout all build the provider via the injected factory.

    The module-level ``CodexCredentialProvider`` is a bomb here, so reaching
    "connected" at all proves no route constructed it directly.
    """
    app, factory = injected_app

    status = await web_api.provider_auth_status("openai_codex")
    assert status["configured"] is False
    assert len(factory.calls) == 1
    # Status/logout do not drive a browser, so they pass no callback.
    assert factory.calls[-1]["authorization_callback"] is None

    started = await web_api.start_provider_auth("openai_codex", _request(app))
    assert started["state"] == "pending"
    # Browser login needs the authorization URL surfaced - the callback is wired.
    login_call = next(call for call in factory.calls if call["authorization_callback"] is not None)
    assert callable(login_call["authorization_callback"])

    task = _sessions(app)["openai_codex"].task
    assert task is not None
    await task

    connected = await web_api.provider_auth_status("openai_codex")
    assert connected["state"] == "connected"
    assert connected["configured"] is True

    disconnected = await web_api.logout_provider("openai_codex", _request(app))
    assert disconnected["configured"] is False
    # Every provider the routes touched came from the injected factory.
    assert len(factory.calls) >= 3


@pytest.mark.asyncio
async def test_missing_factory_falls_back_to_default_provider(monkeypatch) -> None:
    """Without an injected factory the default is used, so old callers still work."""
    _FakeCodexCredentials.connected = False
    monkeypatch.setattr(web_api, "CodexCredentialProvider", _FakeCodexCredentials)
    monkeypatch.setattr(web_api, "_refresh_inference_runtime", AsyncMock())
    app = FastAPI()
    attach(app, ApiServices(web_runtime=web_api.WebRuntime()))
    with bind_services(services_of(app)):
        status = await web_api.provider_auth_status("openai_codex")
    assert status["configured"] is False


def test_configure_wires_a_default_factory(tmp_path) -> None:
    """`configure(...)` always leaves a callable factory on the app's services."""
    app = FastAPI()
    web_api.configure(
        app,
        orchestrator=object(),
        ledger=object(),
        agent_registry=object(),
        job_processor=object(),
        cron_store=object(),
        approval_store=object(),
        north_settings=object(),
        agent_run_store=object(),
        north_home=tmp_path,
    )
    factory = services_of(app).codex_credentials_factory
    assert callable(factory)


def test_configure_preserves_an_injected_factory(tmp_path) -> None:
    """A caller-supplied factory is not overwritten by the default."""
    sentinel = _RecordingFactory()
    app = FastAPI()
    web_api.configure(
        app,
        orchestrator=object(),
        ledger=object(),
        agent_registry=object(),
        job_processor=object(),
        cron_store=object(),
        approval_store=object(),
        north_settings=object(),
        agent_run_store=object(),
        north_home=tmp_path,
        codex_credentials_factory=sentinel,
    )
    assert services_of(app).codex_credentials_factory is sentinel
