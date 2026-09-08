"""Manual routing: one model answers everything, or the call fails saying so.

The point of pinning is that the answer came from the model that was named. A pin
that quietly fell back to something else would make every measurement taken under
it worthless, so the failure is loud and the fallback does not exist.
"""

from __future__ import annotations

import pytest

from config.strategy import NorthSettings, RoutingMode, StrategyMode
from inference.capability import ModelCapability, ModelInfo
from inference.dispatcher import ModelDispatcher
from inference.exceptions import PinnedModelUnavailableError
from inference.models import CompletionRequest, CompletionResponse
from tests.unit.inference._catalog import publish_catalog


class _Provider:
    def __init__(self, name: str, models: dict[str, float]) -> None:
        self.name = name
        self.calls: list[str] = []
        self._models = {
            model_id: ModelInfo(
                model_id=model_id,
                provider_name=name,
                capabilities=frozenset({ModelCapability.COMPLETION, ModelCapability.TOOL_CALLS}),
                context_window=400_000,
                cost_per_token=0.0,
                base_quality=quality,
            )
            for model_id, quality in models.items()
        }

    def get_models(self) -> dict[str, ModelInfo]:
        return dict(self._models)

    async def complete(self, model_id: str, request: CompletionRequest) -> CompletionResponse:
        self.calls.append(model_id)
        return CompletionResponse(text="ok", model_used=model_id, tokens_in=1, tokens_out=1, cost_usd=0.0)


def _setup(tmp_path, **routing):
    settings = NorthSettings(tmp_path / "settings.json")
    if routing:
        settings.set_routing(**routing)
    provider = _Provider("openrouter", {"good/strong-model": 0.95, "good/weak-model": 0.20})
    dispatcher = ModelDispatcher(
        [provider],
        north_settings=settings,
        cooldowns_path=tmp_path / "cooldowns.json",
        models_db_path=tmp_path / "models.db",
    )
    publish_catalog(dispatcher)
    return dispatcher, provider, settings


async def _ask(dispatcher) -> str:
    response = await dispatcher.complete(CompletionRequest(prompt="hi", component="coder"))
    return response.model_used


@pytest.mark.asyncio
async def test_auto_picks_the_strongest_model(tmp_path) -> None:
    dispatcher, _provider, _settings = _setup(tmp_path)
    assert await _ask(dispatcher) == "good/strong-model"


@pytest.mark.asyncio
async def test_manual_routes_to_the_named_model_however_weak(tmp_path) -> None:
    """The whole feature: the ranking is skipped, not merely overruled."""
    dispatcher, provider, _settings = _setup(tmp_path, mode=RoutingMode.MANUAL, model="good/weak-model")
    assert await _ask(dispatcher) == "good/weak-model"
    assert provider.calls == ["good/weak-model"]


@pytest.mark.asyncio
async def test_a_pin_nothing_matches_fails_loudly(tmp_path) -> None:
    dispatcher, provider, _settings = _setup(tmp_path, mode=RoutingMode.MANUAL, model="model-that-left")
    with pytest.raises(PinnedModelUnavailableError):
        await _ask(dispatcher)
    assert provider.calls == []  # never quietly served by something else


@pytest.mark.asyncio
async def test_switching_back_to_auto_releases_the_pin(tmp_path) -> None:
    """The model is remembered, so returning to manual needs no second choice."""
    dispatcher, _provider, settings = _setup(tmp_path, mode=RoutingMode.MANUAL, model="good/weak-model")
    settings.set_routing(mode=RoutingMode.AUTO)

    assert await _ask(dispatcher) == "good/strong-model"
    assert settings.routing_model == "good/weak-model"  # kept
    assert settings.pinned_model == ""  # but not applied


@pytest.mark.asyncio
async def test_a_provider_qualified_pin_is_honoured(tmp_path) -> None:
    dispatcher, _provider, _settings = _setup(tmp_path, mode=RoutingMode.MANUAL, model="openrouter:good/weak-model")
    assert await _ask(dispatcher) == "good/weak-model"


@pytest.mark.asyncio
async def test_a_pin_on_another_provider_matches_nothing(tmp_path) -> None:
    dispatcher, _provider, _settings = _setup(tmp_path, mode=RoutingMode.MANUAL, model="groq:good/weak-model")
    with pytest.raises(PinnedModelUnavailableError):
        await _ask(dispatcher)


def test_power_is_not_offered_while_a_model_is_pinned(tmp_path) -> None:
    """A chain of one has nothing to order, so the dial must not appear to act."""
    dispatcher, _provider, settings = _setup(tmp_path, mode=RoutingMode.MANUAL, model="good/weak-model")
    settings.set_power(StrategyMode.SPORT)
    assert dispatcher._pinned_model() == "good/weak-model"
    assert dispatcher._power_mode() is None

    settings.set_routing(mode=RoutingMode.AUTO)
    assert dispatcher._power_mode() == "sport"
