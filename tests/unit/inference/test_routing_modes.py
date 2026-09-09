"""Routing readiness: what happens before the catalog north ranks from has loaded.

There is one router now. The pool router that used to serve calls while the
facts catalog was still being fetched is gone, so this window has to announce
itself rather than being quietly covered by a second set of selection rules.
"""

from __future__ import annotations

import asyncio

import pytest

from inference.capability import ModelCapability, ModelInfo
from inference.dispatcher import ModelDispatcher
from inference.exceptions import RoutingNotReadyError
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


def _dispatcher(tmp_path, *, persist: bool = True) -> ModelDispatcher:
    """A dispatcher over one fake provider. ``persist=False`` gives it nowhere to store."""
    if not persist:
        return ModelDispatcher([_FakeProvider(_models())])
    return ModelDispatcher(
        [_FakeProvider(_models())],
        cooldowns_path=tmp_path / "cooldowns.json",
        models_db_path=tmp_path / "models.db",
    )


def test_a_chain_router_is_built_whenever_there_is_a_catalog_to_open(tmp_path) -> None:
    dispatcher = _dispatcher(tmp_path)
    assert dispatcher._chain_router is not None
    assert dispatcher.routing_decisions() == []


def test_no_catalog_path_means_no_router_at_all(tmp_path) -> None:
    """A dispatcher told not to persist must not reach for the user's real catalog."""
    assert _dispatcher(tmp_path, persist=False)._chain_router is None


@pytest.mark.asyncio
async def test_a_completion_before_the_catalog_loads_says_so(tmp_path) -> None:
    """The failure a person can act on: wait, rather than a silent second opinion."""
    dispatcher = _dispatcher(tmp_path)
    with pytest.raises(RoutingNotReadyError):
        await dispatcher.complete(CompletionRequest(prompt="hello", component="coder"))


@pytest.mark.asyncio
async def test_the_same_is_true_with_no_router_at_all(tmp_path) -> None:
    dispatcher = _dispatcher(tmp_path, persist=False)
    with pytest.raises(RoutingNotReadyError):
        await dispatcher.complete(CompletionRequest(prompt="hello", component="coder"))


@pytest.mark.asyncio
async def test_an_image_in_the_tool_loop_requires_a_vision_model(tmp_path) -> None:
    """Agents send screenshots as image_url parts, not through request.images."""
    from inference.dispatcher import _messages_carry_images

    with_image = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "data:..."}}],
        }
    ]
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
