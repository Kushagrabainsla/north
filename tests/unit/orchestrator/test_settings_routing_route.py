"""The settings endpoint the web page drives for the routing dial."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import orchestrator.api.settings as api
from config.strategy import NorthSettings, RoutingMode
from orchestrator.api_context import ApiServices, bind_services


@pytest.fixture
def settings(tmp_path) -> NorthSettings:
    return NorthSettings(tmp_path / "settings.json")


async def update(**body):
    return await api.update_settings(api.SettingsUpdate(**body))


@pytest.mark.asyncio
async def test_routing_is_reported_with_the_other_dials(settings) -> None:
    with bind_services(ApiServices(north_settings=settings)):
        out = await api.get_settings()
    assert out.routing == "auto" and out.model == ""


@pytest.mark.asyncio
async def test_a_model_can_be_pinned(settings) -> None:
    with bind_services(ApiServices(north_settings=settings)):
        out = await update(routing="manual", model="groq:qwen3-32b")

    assert out.routing == "manual" and out.model == "groq:qwen3-32b"
    assert settings.pinned_model == "groq:qwen3-32b"


@pytest.mark.asyncio
async def test_manual_without_a_model_is_refused(settings) -> None:
    """Otherwise the refusal arrives on the user's next task instead of here."""
    with bind_services(ApiServices(north_settings=settings)), pytest.raises(HTTPException) as exc:
        await update(routing="manual")
    assert exc.value.status_code == 422
    assert settings.routing_mode is RoutingMode.AUTO


@pytest.mark.asyncio
async def test_manual_is_allowed_when_a_model_was_chosen_earlier(settings) -> None:
    settings.set_routing(model="groq:qwen3-32b")
    with bind_services(ApiServices(north_settings=settings)):
        out = await update(routing="manual")
    assert out.routing == "manual" and settings.pinned_model == "groq:qwen3-32b"


@pytest.mark.asyncio
async def test_an_unknown_mode_is_a_422(settings) -> None:
    with bind_services(ApiServices(north_settings=settings)), pytest.raises(HTTPException) as exc:
        await update(routing="sideways")
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_switching_to_auto_keeps_the_model_for_next_time(settings) -> None:
    with bind_services(ApiServices(north_settings=settings)):
        await update(routing="manual", model="groq:qwen3-32b")
        out = await update(routing="auto")

    assert out.routing == "auto" and out.model == "groq:qwen3-32b"
    assert settings.pinned_model == ""


@pytest.mark.asyncio
async def test_the_catalog_is_grouped_by_provider(tmp_path) -> None:
    """What the manual picker offers: provider first, then that provider's models."""
    import orchestrator.api.inference as inference_api
    from inference.models import ModelEntry, ModelPool

    class _Router:
        def current_pools(self):
            # The same model appears in several pools; the picker must list it once.
            entries = [ModelEntry(id="qwen3-32b", provider="groq"), ModelEntry(id="gpt-5.6-sol", provider="codex")]
            return {
                "reasoning": ModelPool(name="reasoning", models=entries),
                "speed": ModelPool(name="speed", models=[entries[0]]),
            }

    with bind_services(ApiServices(inference_router=_Router())):
        catalog = await inference_api.inference_catalog()

    assert [row.provider for row in catalog] == ["codex", "groq"]  # sorted, for a stable list
    assert [row.models for row in catalog] == [["gpt-5.6-sol"], ["qwen3-32b"]]
