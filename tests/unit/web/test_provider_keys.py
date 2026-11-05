"""A provider key is checked by trying it, saving one keeps the models listed, and a wrong one can go.

In the issue #76 test (P6) a made-up Gemini key was answered "Provider credentials
saved and runtime refreshed." and shown "Ready AIza…test", while north.log said
"Gemini /models returned 400"; nothing in the dashboard could remove it. Saving
any key (P5) swapped in a router with no models: for about two and a half
minutes /health said "starting", Settings "No models are reachable", and north
status counted 0 models, until the next pool refresh.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI

from config.runtime import set_runtime
from inference.capability import ModelCapability, ModelInfo
from inference.dispatcher import ModelDispatcher
from orchestrator.api_context import ApiServices, attach, bind_services, services_of
from web import api as web_api


class _Provider:
    """A provider whose model list answers *models*, or raises *refusal*."""

    def __init__(self, name: str, refusal: str = "") -> None:
        self.name = name
        self._refusal = refusal
        self._models: dict[str, ModelInfo] = {}

    def get_models(self) -> dict[str, ModelInfo]:
        return dict(self._models)

    async def refresh(self) -> None:
        if self._refusal:
            raise RuntimeError(self._refusal)
        self._models = {
            "m1": ModelInfo(
                model_id="m1",
                provider_name=self.name,
                capabilities=frozenset({ModelCapability.COMPLETION}),
                context_window=1000,
                cost_per_token=0.0,
                base_quality=1.0,
            )
        }


async def test_a_key_the_provider_rejects_is_failing_with_its_reason(tmp_path: Path) -> None:
    router = ModelDispatcher(
        [_Provider("gemini", refusal="Gemini /models returned 400"), _Provider("groq")],
        cooldowns_path=tmp_path / "cooldowns.json",
    )

    await router.refresh_catalog()

    assert router.provider_state("gemini") == ("failing", "Gemini /models returned 400")
    assert router.provider_state("groq") == ("ready", "")


class _Deps:
    def __init__(self, router: object) -> None:
        self.inference_router = router
        self.cost_tracker = MagicMock()
        self.north_settings = None
        self.confidence_tracker = None


async def test_saving_a_key_swaps_in_a_router_only_once_it_has_its_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = SimpleNamespace(name="old")
    deps = _Deps(old)
    filled = asyncio.Event()

    class _New:
        async def refresh_catalog(self) -> None:
            await filled.wait()

    new = _New()
    monkeypatch.setattr("inference.runtime.rebuild_runtime_router", lambda _deps: new)
    monkeypatch.setattr("config.settings.reload_settings", lambda: None)
    monkeypatch.setattr(web_api, "_PROVIDER_CHECK_SECONDS", 0.05)
    app = FastAPI()
    attach(app, ApiServices(inference_router=old))
    set_runtime(deps)
    try:
        await web_api._refresh_inference_runtime(app)
        assert deps.inference_router is old, "the old router serves until the new one has its catalog"

        filled.set()
        await asyncio.sleep(0.01)
        assert deps.inference_router is new and services_of(app).inference_router is new
    finally:
        set_runtime(None)


async def test_saving_a_key_says_what_trying_it_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = SimpleNamespace(provider_state=lambda name: ("unknown", ""))
    new = SimpleNamespace(provider_state=lambda name: ("failing", "Gemini /models returned 400"))

    async def refreshed(app: FastAPI) -> object:
        return new  # swapped in; this request's own services still hold the old router

    monkeypatch.setattr(web_api, "_refresh_inference_runtime", refreshed)
    monkeypatch.delenv("NORTH_GEMINI_API_KEY", raising=False)
    app = FastAPI()
    attach(app, ApiServices(north_home=tmp_path, inference_router=old))
    try:
        with bind_services(services_of(app)):
            saved = await web_api.update_provider(
                "gemini", web_api.ProviderCredentialUpdate(api_key="AIza-fake-test"), SimpleNamespace(app=app)
            )
    finally:
        os.environ.pop("NORTH_GEMINI_API_KEY", None)

    assert (saved["state"], saved["state_detail"]) == ("failing", "Gemini /models returned 400")


async def test_a_key_can_be_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def refreshed(app: FastAPI) -> None:
        return None

    monkeypatch.setattr(web_api, "_refresh_inference_runtime", refreshed)
    (tmp_path / ".env").write_text("NORTH_GEMINI_API_KEY=AIza-fake-test\nNORTH_GROQ_API_KEY=gsk_real\n")
    monkeypatch.setenv("NORTH_GEMINI_API_KEY", "AIza-fake-test")
    app = FastAPI()
    attach(app, ApiServices(north_home=tmp_path))
    with bind_services(services_of(app)):
        removed = await web_api.remove_provider("gemini", SimpleNamespace(app=app))

    assert removed["configured"] is False
    assert (tmp_path / ".env").read_text() == "NORTH_GROQ_API_KEY=gsk_real\n"
    assert "NORTH_GEMINI_API_KEY" not in os.environ
    assert oct((tmp_path / ".env").stat().st_mode & 0o777) == "0o600"
