"""Unit tests for dynamic capability pools, automated classification, and concurrent refresh."""

from __future__ import annotations

import pytest

from inference.capability import ModelCapability, ModelInfo, capabilities_from_model_id
from inference.dispatcher import ModelDispatcher
from inference.models import (
    CompletionRequest,
    CompletionResponse,
)
from inference.provider import Provider
from tests.unit.inference._catalog import publish_catalog


class _DummyProvider(Provider):
    def __init__(self, name: str, models: dict[str, ModelInfo], should_fail_refresh: bool = False):
        self._name = name
        self._models = dict(models)
        self.should_fail_refresh = should_fail_refresh
        self.refresh_calls = 0

    @property
    def name(self) -> str:
        return self._name

    def get_models(self) -> dict[str, ModelInfo]:
        return self._models

    async def refresh(self) -> None:
        self.refresh_calls += 1
        if self.should_fail_refresh:
            raise RuntimeError(f"Simulated network failure on {self._name}")

    async def complete(self, model_id: str, request: CompletionRequest) -> CompletionResponse:
        return CompletionResponse(
            text="hello from " + model_id,
            model_used=model_id,
            tokens_in=10,
            tokens_out=5,
            cost_usd=0.0,
        )

    async def complete_stream(self, model_id, request):
        yield "chunk"

    async def complete_with_tools(self, model_id, request, token_callback=None):
        raise NotImplementedError

    async def transcribe(self, model_id, request):
        raise NotImplementedError

    async def embed(self, model_id, request):
        raise NotImplementedError


def _make_info(
    model_id: str,
    provider: str,
    caps: frozenset[ModelCapability],
    quality: float = 0.5,
    cost: float = 0.0,
    ctx: int = 8192,
) -> ModelInfo:
    return ModelInfo(
        model_id=model_id,
        provider_name=provider,
        capabilities=caps,
        context_window=ctx,
        cost_per_token=cost,
        base_quality=quality,
    )


def test_capability_classification_across_modalities():
    """Verify capabilities_from_model_id correctly classifies all modality and performance tiers."""
    # Transcription & Audio
    whisper_caps = capabilities_from_model_id("whisper-large-v3", "groq")
    assert ModelCapability.TRANSCRIPTION in whisper_caps
    assert ModelCapability.AUDIO in whisper_caps

    # Audio / TTS
    tts_caps = capabilities_from_model_id("gemini-2.5-flash-preview-tts", "gemini")
    assert ModelCapability.AUDIO in tts_caps

    # Embeddings
    embed_caps = capabilities_from_model_id("gemini-embedding-001", "gemini")
    assert ModelCapability.EMBEDDING in embed_caps

    # Vision / Multimodal
    vision_caps = capabilities_from_model_id("gemini-2.5-pro", "gemini")
    assert ModelCapability.VISION in vision_caps
    assert ModelCapability.COMPLETION in vision_caps
    assert ModelCapability.TOOL_CALLS in vision_caps
    assert ModelCapability.REASONING in vision_caps

    # Deep Reasoning
    sonnet_caps = capabilities_from_model_id("claude-sonnet-5", "opencode_zen")
    assert ModelCapability.REASONING in sonnet_caps
    assert ModelCapability.COMPLETION in sonnet_caps

    # High Speed
    groq_70b_caps = capabilities_from_model_id("llama-3.3-70b-versatile", "groq")
    assert ModelCapability.SPEED in groq_70b_caps
    assert ModelCapability.REASONING in groq_70b_caps

    flash_caps = capabilities_from_model_id("gemini-2.5-flash", "gemini")
    assert ModelCapability.SPEED in flash_caps
    assert ModelCapability.VISION in flash_caps


@pytest.mark.asyncio
async def test_concurrent_refresh_with_graceful_failure_retention(tmp_path):
    """Verify refresh_pools runs concurrently and retains existing catalog if one provider fails."""
    m1 = _make_info("qwen3.6-27b", "groq", frozenset({ModelCapability.COMPLETION, ModelCapability.SPEED}))
    m2 = _make_info("gemini-2.5-flash", "gemini", frozenset({ModelCapability.COMPLETION, ModelCapability.SPEED}))

    p1 = _DummyProvider("groq", {"qwen3.6-27b": m1})
    p2 = _DummyProvider("gemini", {"gemini-2.5-flash": m2}, should_fail_refresh=True)

    disp = ModelDispatcher([p1, p2], cooldowns_path=tmp_path / "cooldowns.json")

    # Initial state has both
    assert ("groq", "qwen3.6-27b") in disp._registry
    assert ("gemini", "gemini-2.5-flash") in disp._registry

    # Refresh: p1 succeeds, p2 fails
    await disp.refresh_pools()

    assert p1.refresh_calls == 1
    assert p2.refresh_calls == 1

    # Gemini model was retained despite refresh error
    assert ("groq", "qwen3.6-27b") in disp._registry
    assert ("gemini", "gemini-2.5-flash") in disp._registry


@pytest.mark.asyncio
async def test_a_pool_name_now_only_orders_the_chain(tmp_path):
    """``model_pool`` survives on agent configs, but it no longer picks the members.

    It is read as an ordering hint: "reasoning" asks for the strongest model,
    "speed" for the cheapest. Both chains contain both models - which is the whole
    change from pools, where asking for speed meant a different, smaller list.
    """
    strong = _make_info(
        "claude-opus-5",
        "zen",
        frozenset({ModelCapability.COMPLETION, ModelCapability.TOOL_CALLS}),
        quality=0.95,
        cost=1e-5,
        ctx=400_000,
    )
    cheap = _make_info(
        "groq-compound-mini",
        "groq",
        frozenset({ModelCapability.COMPLETION, ModelCapability.TOOL_CALLS}),
        quality=0.40,
        cost=0.0,
        ctx=400_000,
    )

    disp = ModelDispatcher(
        [_DummyProvider("zen", {"claude-opus-5": strong}), _DummyProvider("groq", {"groq-compound-mini": cheap})],
        cooldowns_path=tmp_path / "cooldowns.json",
        models_db_path=tmp_path / "models.db",
    )
    publish_catalog(disp)

    reasoning = await disp.complete(CompletionRequest(prompt="design it", component="coder", pool="reasoning"))
    assert reasoning.model_used == "claude-opus-5"

    speed = await disp.complete(CompletionRequest(prompt="classify", component="router", pool="speed"))
    assert speed.model_used == "groq-compound-mini"


def test_current_pools_exposes_all_capability_pools(tmp_path):
    """Verify current_pools() returns all capability pools for CLI and UI observability."""
    m1 = _make_info("opus", "zen", frozenset({ModelCapability.REASONING, ModelCapability.COMPLETION}), quality=0.95)
    m2 = _make_info(
        "flash",
        "gemini",
        frozenset({ModelCapability.SPEED, ModelCapability.COMPLETION, ModelCapability.VISION}),
        quality=0.7,
    )
    m3 = _make_info("whisper", "groq", frozenset({ModelCapability.TRANSCRIPTION, ModelCapability.AUDIO}))
    m4 = _make_info("embed", "gemini", frozenset({ModelCapability.EMBEDDING}))

    p = _DummyProvider("test", {"opus": m1, "flash": m2, "whisper": m3, "embed": m4})
    disp = ModelDispatcher([p], cooldowns_path=tmp_path / "cooldowns.json")

    pools = disp.current_pools()
    assert "reasoning" in pools
    assert "speed" in pools
    assert "tool_calling" in pools
    assert "vision" in pools
    assert "audio" in pools
    assert "embeddings" in pools
    assert "fast_cheap" in pools
    assert "high_volume" in pools
    assert "free_fallback" in pools

    # Check contents
    reasoning_ids = [m.id for m in pools["reasoning"].models]
    assert "opus" in reasoning_ids

    speed_ids = [m.id for m in pools["speed"].models]
    assert "flash" in speed_ids

    audio_ids = [m.id for m in pools["audio"].models]
    assert "whisper" in audio_ids

    embed_ids = [m.id for m in pools["embeddings"].models]
    assert "embed" in embed_ids
