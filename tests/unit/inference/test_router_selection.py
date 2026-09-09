"""End-to-end router selection: best-model pick, fallthrough, cooldown, exhaustion.

These drive the REAL ModelDispatcher with fake multi-provider catalogs (same
Provider interface production uses), so the routing decisions asserted here are
exactly what ships - just without hitting the network.

What this proves:
  * the router picks the highest-scoring model that meets the part's needs
  * a failing model falls through to the next candidate on another provider
  * a rate-limited model is cooled down and skipped on the next call
  * price breaks a tie between models of equal measured score
  * every candidate failing is reported, not papered over
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from config.strategy import NorthSettings
from inference.capability import ModelCapability, ModelInfo
from inference.dispatcher import ModelDispatcher
from inference.exceptions import AllModelsRateLimitedError, InferenceError, ModelRateLimitedError
from inference.models import (
    CompletionRequest,
    CompletionResponse,
    EmbedRequest,
    EmbedResponse,
    PoolPriority,
    ToolCallRequest,
    ToolCallResponse,
    TranscriptionRequest,
    TranscriptionResponse,
)
from tests.unit.inference._catalog import publish_catalog


def _mi(model_id: str, *, provider: str, quality: float, cost: float = 0.0, ctx: int = 400_000) -> ModelInfo:
    return ModelInfo(
        model_id=model_id,
        provider_name=provider,
        capabilities=frozenset({ModelCapability.COMPLETION, ModelCapability.TOOL_CALLS}),
        context_window=ctx,
        cost_per_token=cost,
        base_quality=quality,
    )


def _resp(model: str) -> CompletionResponse:
    return CompletionResponse(text="ok", model_used=model, tokens_in=1, tokens_out=1, cost_usd=0.0)


class _Catalog:
    """Fake Provider: arbitrary catalog + per-model responder (may raise)."""

    def __init__(self, name: str, models: list[ModelInfo], responder) -> None:
        self._name = name
        self._models = {m.model_id: m for m in models}
        self._responder = responder
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    def get_models(self) -> dict[str, ModelInfo]:
        return dict(self._models)

    async def complete(self, model_id: str, request: CompletionRequest) -> CompletionResponse:
        self.calls.append(model_id)
        return self._responder(model_id, request)

    async def complete_with_tools(
        self,
        model_id: str,
        request: ToolCallRequest,
        token_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> ToolCallResponse:
        self.calls.append(model_id)
        return self._responder(model_id, request)  # type: ignore[return-value]

    async def embed(self, model_id: str, request: EmbedRequest) -> EmbedResponse:  # pragma: no cover
        raise NotImplementedError

    async def transcribe(self, model_id: str, request: TranscriptionRequest) -> TranscriptionResponse:  # noqa: E501
        raise NotImplementedError


def _disp(providers: list[_Catalog], tmp_path) -> ModelDispatcher:
    """A dispatcher with a catalog already published - i.e. one that can route."""
    dispatcher = ModelDispatcher(
        providers=providers,
        north_settings=NorthSettings(tmp_path / "settings.json"),
        cooldowns_path=tmp_path / "cd.json",
        models_db_path=tmp_path / "models.db",
    )
    publish_catalog(dispatcher)
    return dispatcher


@pytest.mark.asyncio
async def test_the_highest_scoring_model_is_chosen(tmp_path):
    # Two providers, each with one model. The chain ranks on measured score, so
    # the better model wins regardless of which provider happens to own it.
    a = _Catalog("openrouter", [_mi("gpt-oss-20b", provider="openrouter", quality=0.4)], lambda m, r: _resp(m))
    b = _Catalog("opencode_zen", [_mi("claude-opus-4-8", provider="opencode_zen", quality=0.95)], lambda m, r: _resp(m))
    disp = _disp([a, b], tmp_path)
    resp = await disp.complete(CompletionRequest(prompt="think", priority=PoolPriority.HIGH, component="coder"))
    # claude-opus-4-8 (family tier 0.96) must beat gpt-oss-20b (0.42).
    assert resp.model_used == "claude-opus-4-8"


@pytest.mark.asyncio
async def test_failure_falls_through_to_other_provider(tmp_path):
    # The best model is on provider A but raises; the router must fall through
    # to provider B's model instead of erroring out.
    def a_responder(model_id, request):
        if model_id == "claude-opus-4-8":
            raise InferenceError("opus exploded")
        return _resp(model_id)

    a = _Catalog("opencode_zen", [_mi("claude-opus-4-8", provider="opencode_zen", quality=0.95)], a_responder)
    b = _Catalog("groq", [_mi("llama-3.1-8b-instant", provider="groq", quality=0.7)], lambda m, r: _resp(m))
    disp = _disp([a, b], tmp_path)
    resp = await disp.complete(CompletionRequest(prompt="think", priority=PoolPriority.HIGH, component="coder"))
    assert resp.model_used == "llama-3.1-8b-instant"
    # Provider A's broken model was attempted once, then abandoned.
    assert a.calls == ["claude-opus-4-8"]


@pytest.mark.asyncio
async def test_rate_limited_model_is_cooldown_and_skipped(tmp_path):
    # Provider A rate-limits; the router cools A's model down and answers from B.
    def a_responder(model_id, request):
        raise ModelRateLimitedError(model_id, "opencode_zen", retry_after=60.0)

    a = _Catalog("opencode_zen", [_mi("claude-opus-4-8", provider="opencode_zen", quality=0.95)], a_responder)
    b = _Catalog("groq", [_mi("llama-3.1-8b-instant", provider="groq", quality=0.7)], lambda m, r: _resp(m))
    disp = _disp([a, b], tmp_path)

    resp = await disp.complete(CompletionRequest(prompt="think", priority=PoolPriority.HIGH, component="coder"))
    assert resp.model_used == "llama-3.1-8b-instant"

    # Immediately retry: A must be skipped (cooldown active), so B answers again
    # without even contacting A.
    resp2 = await disp.complete(CompletionRequest(prompt="think", priority=PoolPriority.HIGH, component="coder"))
    assert resp2.model_used == "llama-3.1-8b-instant"
    assert a.calls == ["claude-opus-4-8"]  # tried once, then cooled; never retried


@pytest.mark.asyncio
async def test_price_breaks_a_tie_between_equal_models(tmp_path):
    # Cost is never blended into the score - it decides only between models the
    # measurements cannot tell apart, which is what makes the cheaper one win here.
    paid = _Catalog(
        "openrouter", [_mi("or-paid", provider="openrouter", quality=0.7, cost=0.001)], lambda m, r: _resp(m)
    )
    free = _Catalog(
        "opencode_zen",
        [_mi("zen-free", provider="opencode_zen", quality=0.7, cost=0.0, ctx=400_000)],
        lambda m, r: _resp(m),
    )
    disp = _disp([paid, free], tmp_path)
    resp = await disp.complete(CompletionRequest(prompt="chat", priority=PoolPriority.MEDIUM, component="general"))
    assert resp.model_used == "zen-free"


@pytest.mark.asyncio
async def test_all_models_exhausted(tmp_path):
    # Every candidate fails -> the router reports total exhaustion, it does not
    # silently return garbage.
    def boom(model_id, request):
        return (_ for _ in ()).throw(InferenceError("down"))

    a = _Catalog("openrouter", [_mi("or-best", provider="openrouter", quality=0.95)], boom)
    b = _Catalog("groq", [_mi("groq-mid", provider="groq", quality=0.7)], boom)
    disp = _disp([a, b], tmp_path)
    with pytest.raises(AllModelsRateLimitedError):
        await disp.complete(CompletionRequest(prompt="think", priority=PoolPriority.HIGH, component="coder"))
