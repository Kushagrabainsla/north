"""NORTH_ROUTING selects which router serves a call, and whether it is audited."""

from __future__ import annotations

import asyncio

import pytest

from inference.capability import ModelCapability, ModelInfo
from inference.dispatcher import ModelDispatcher
from inference.models import CompletionRequest, CompletionResponse


class _FakeProvider:
    name = "openrouter"

    def __init__(self, models: dict[str, ModelInfo]) -> None:
        self._models = models

    def get_models(self) -> dict[str, ModelInfo]:
        return dict(self._models)

    async def refresh(self) -> None:  # pragma: no cover - never called here
        return None

    async def complete(self, model_id: str, request: CompletionRequest) -> CompletionResponse:
        return CompletionResponse(text="ok", model_used=model_id, tokens_in=1, tokens_out=1, cost_usd=0.0)


def _models() -> dict[str, ModelInfo]:
    return {
        model_id: ModelInfo(
            model_id=model_id,
            provider_name="openrouter",
            capabilities=frozenset({ModelCapability.COMPLETION, ModelCapability.TOOL_CALLS}),
            context_window=400_000,
            cost_per_token=cost,
            base_quality=0.5,
        )
        for model_id, cost in (("z-ai/glm-5.2:free", 0.0), ("anthropic/claude-opus-5", 2.5e-5))
    }


def _dispatcher(tmp_path, mode: str) -> ModelDispatcher:
    return ModelDispatcher(
        [_FakeProvider(_models())],
        cooldowns_path=tmp_path / "cooldowns.json",
        models_db_path=tmp_path / "models.db",
        routing_mode=mode,
    )


def test_legacy_mode_builds_no_chain_router(tmp_path) -> None:
    dispatcher = _dispatcher(tmp_path, "legacy")
    assert dispatcher._chain_router is None
    assert dispatcher.uses_chain_routing is False
    assert dispatcher.routing_decisions() == []


def test_an_unrecognised_mode_falls_back_to_legacy(tmp_path) -> None:
    assert _dispatcher(tmp_path, "nonsense")._chain_router is None


def test_shadow_mode_prepares_a_chain_but_does_not_route_on_it(tmp_path) -> None:
    dispatcher = _dispatcher(tmp_path, "shadow")
    assert dispatcher._chain_router is not None
    assert dispatcher.uses_chain_routing is False


def test_chain_mode_waits_for_a_catalog_before_taking_over(tmp_path) -> None:
    """An empty models.db must never mean "no models available"."""
    dispatcher = _dispatcher(tmp_path, "chain")
    assert dispatcher._chain_router is not None
    assert dispatcher.uses_chain_routing is False  # nothing fetched yet


@pytest.mark.asyncio
async def test_the_legacy_path_still_serves_when_there_is_no_catalog(tmp_path) -> None:
    dispatcher = _dispatcher(tmp_path, "chain")
    response = await dispatcher.complete(CompletionRequest(prompt="hello", component="coder"))
    assert response.model_used in _models()


@pytest.mark.asyncio
async def test_an_image_in_the_tool_loop_requires_a_vision_model(tmp_path) -> None:
    """Agents send screenshots as image_url parts, not through request.images."""
    from inference.dispatcher import _messages_carry_images

    with_image = [{"role": "user", "content": [{"type": "text", "text": "look"},
                                               {"type": "image_url", "image_url": {"url": "data:..."}}]}]
    assert _messages_carry_images(with_image) is True
    assert _messages_carry_images([{"role": "user", "content": "plain text"}]) is False
    assert _messages_carry_images([{"role": "user", "content": [{"type": "text", "text": "x"}]}]) is False


def test_the_litellm_disk_cache_survives_a_restart(tmp_path) -> None:
    """A fresh cache written by the last process must not be re-downloaded."""
    import json

    from inference.facts.sources.litellm import LiteLLMSource

    fetches = 0

    class _Client:
        async def get(self, url):
            nonlocal fetches
            fetches += 1
            raise AssertionError("the cached copy should have been used")

    cache = tmp_path / "litellm_models.json"
    cache.write_text(json.dumps({"m": {"mode": "chat", "max_input_tokens": 2000}}))

    facts = asyncio.run(LiteLLMSource(cache, client=_Client()).load())
    assert fetches == 0
    assert [record.canonical_id for record in facts] == ["m"]
