"""Local embeddings: available without a key, and chosen consistently."""

from __future__ import annotations

import pytest

from inference.capability import ModelCapability, ModelInfo
from inference.dispatcher import ModelDispatcher
from inference.models import EmbedRequest, EmbedResponse
from inference.providers.local_embeddings import LocalEmbeddingProvider
from inference.registry import PROVIDER_DEFINITIONS, AuthKind, get_provider_definition


class _RemoteEmbedder:
    """Stands in for Gemini - the only remote provider that sells embeddings."""

    name = "gemini"

    def get_models(self) -> dict[str, ModelInfo]:
        return {
            "text-embedding-004": ModelInfo(
                model_id="text-embedding-004",
                provider_name="gemini",
                capabilities=frozenset({ModelCapability.EMBEDDING}),
                context_window=0,
                cost_per_token=0.0,
                base_quality=0.5,
            )
        }

    async def refresh(self) -> None:
        return None

    async def embed(self, model_id: str, request: EmbedRequest) -> EmbedResponse:
        return EmbedResponse(
            embeddings=[[0.5] * 8 for _ in request.texts], model_used=model_id, cost_usd=0.0
        )


class _StubLocal(LocalEmbeddingProvider):
    """The real provider with the model swapped for a deterministic stub."""

    def _load_sync(self):
        class _Model:
            @staticmethod
            def encode(texts):
                return [[float(len(t))] * 4 for t in texts]

        return _Model()


def test_the_local_provider_needs_no_credential() -> None:
    definition = get_provider_definition("local")
    assert definition.auth_kind is AuthKind.LOCAL
    assert definition.is_configured(None) is True
    assert definition.is_configured(object()) is True


def test_it_is_registered_ahead_of_the_remote_providers() -> None:
    orders = {d.id: d.fallback_order for d in PROVIDER_DEFINITIONS}
    assert orders["local"] < min(v for k, v in orders.items() if k != "local")


def test_its_model_is_known_before_the_weights_are_loaded() -> None:
    """Vector stores stamp themselves at startup, before any embedding call."""
    provider = LocalEmbeddingProvider()
    assert provider.model_id
    assert provider.get_models() == {}  # nothing loaded yet


@pytest.mark.asyncio
async def test_it_serves_embeddings_at_no_cost() -> None:
    provider = _StubLocal()
    await provider.refresh()
    assert list(provider.get_models()) == [provider.model_id]
    response = await provider.embed(provider.model_id, EmbedRequest(texts=["ab", "cde"], component="embed"))
    assert response.embeddings == [[2.0] * 4, [3.0] * 4]
    assert response.cost_usd == 0.0
    assert response.model_used == provider.model_id


@pytest.mark.asyncio
async def test_an_empty_batch_does_not_touch_the_model() -> None:
    provider = _StubLocal()
    await provider.refresh()
    assert (await provider.embed("m", EmbedRequest(texts=[], component="embed"))).embeddings == []


@pytest.mark.asyncio
async def test_a_model_that_will_not_load_degrades_instead_of_raising() -> None:
    """No local model is the state north was in before; it must stay survivable."""

    class _Unloadable(LocalEmbeddingProvider):
        def _load_sync(self):
            return None

    provider = _Unloadable()
    await provider.refresh()  # must not raise
    assert provider.get_models() == {}


@pytest.mark.asyncio
async def test_it_refuses_completions_rather_than_pretending(tmp_path) -> None:
    from inference.exceptions import InferenceError

    provider = LocalEmbeddingProvider()
    with pytest.raises(InferenceError):
        await provider.complete("m", None)


class TestSelection:
    """Embedding choice must be deterministic - alternating models rebuilds every index."""

    def _dispatcher(self, tmp_path, providers) -> ModelDispatcher:
        return ModelDispatcher(providers, cooldowns_path=tmp_path / "cooldowns.json", routing_mode="legacy")

    @pytest.mark.asyncio
    async def test_local_is_chosen_over_an_equally_free_remote(self, tmp_path) -> None:
        local = _StubLocal()
        await local.refresh()
        dispatcher = self._dispatcher(tmp_path, [_RemoteEmbedder(), local])
        for _ in range(10):  # a tie-break would eventually pick the other one
            response = await dispatcher.embed(EmbedRequest(texts=["x"], component="embed"))
            assert response.model_used == local.model_id

    @pytest.mark.asyncio
    async def test_a_remote_still_serves_when_there_is_no_local_model(self, tmp_path) -> None:
        dispatcher = self._dispatcher(tmp_path, [_RemoteEmbedder()])
        response = await dispatcher.embed(EmbedRequest(texts=["x"], component="embed"))
        assert response.model_used == "text-embedding-004"

    def test_the_embedding_model_is_reported_for_stamping(self, tmp_path) -> None:
        local = LocalEmbeddingProvider()
        dispatcher = self._dispatcher(tmp_path, [_RemoteEmbedder(), local])
        assert dispatcher.embedding_model_id() == local.model_id

    def test_it_falls_back_to_the_registry_when_there_is_no_local_provider(self, tmp_path) -> None:
        dispatcher = self._dispatcher(tmp_path, [_RemoteEmbedder()])
        assert dispatcher.embedding_model_id() == "text-embedding-004"
