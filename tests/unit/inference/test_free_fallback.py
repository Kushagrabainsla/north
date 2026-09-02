"""Free-tier fallback: when the primary (paid) pool is exhausted, the dispatcher
must try free models instead of failing with "No models available".

See README 8.x - this is the safety net behind the 413 / out-of-credits handling.
"""
from __future__ import annotations

import pytest

from inference.capability import ModelCapability, ModelInfo
from inference.cooldowns import CooldownStore
from inference.dispatcher import ModelDispatcher
from inference.exceptions import PaymentRequiredError
from inference.model_scorer import ModelScorer, ScoringConfig
from inference.models import CompletionRequest, PoolPriority
from inference.provider import Provider
from inference.provider_health import ProviderHealthTracker
from inference.rate_limit_status import RateLimitStatusStore


class _FakeProvider(Provider):
    def __init__(self, name: str, behaviour: str) -> None:
        self._name = name
        self._behaviour = behaviour  # "pay" | "ok"
        self.called = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self):  # not used in this test
        return PoolPriority.MEDIUM

    async def complete(self, model_id: str, request: CompletionRequest):
        self.called = True
        if self._behaviour == "pay":
            raise PaymentRequiredError(model_id, self._name)
        return type("R", (), {"text": "ok", "model_used": model_id})()

    async def complete_with_tools(self, model_id, request, token_callback=None):
        return self.complete(model_id, request)

    async def transcribe(self, model_id, request):
        raise NotImplementedError

    async def embed(self, model_id, request):
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


def _make_dispatcher() -> ModelDispatcher:
    d = ModelDispatcher.__new__(ModelDispatcher)
    paid = ModelInfo(
        model_id="paid-model", provider_name="opencode_zen",
        capabilities=frozenset({ModelCapability.COMPLETION}),
        context_window=200_000, cost_per_token=0.01, base_quality=0.9,
    )
    free = ModelInfo(
        model_id="free-model", provider_name="openrouter",
        capabilities=frozenset({ModelCapability.COMPLETION}),
        context_window=200_000, cost_per_token=0.0, base_quality=0.5,
    )
    d._registry = {
        ("paid-model", "opencode_zen"): (paid, _FakeProvider("opencode_zen", "pay")),
        ("free-model", "openrouter"): (free, _FakeProvider("openrouter", "ok")),
    }
    d._cooldowns = CooldownStore()
    d._provider_health = ProviderHealthTracker()
    d._rate_limit_status = RateLimitStatusStore()
    d._north_settings = None
    d._confidence_tracker = None
    d._model_confidence = {}
    d._dirty_scores = set()
    d._flush_task = None
    d._providers = []
    d._scorer = ModelScorer(config=ScoringConfig())
    d._sticky = {}
    d._generation = 0
    d._candidate_cache = {}
    # Build the lookups derived from _registry, exactly as _build_registry does.
    d._index_registry()
    return d


@pytest.mark.asyncio
async def test_fallback_to_free_when_paid_exhausted() -> None:
    """All paid candidates fail with PaymentRequiredError; free fallback must serve."""
    d = _make_dispatcher()
    req = CompletionRequest(prompt="hi", component="planner", priority=PoolPriority.HIGH)
    resp = await d.complete(req)
    assert resp.text == "ok"
    assert resp.model_used == "free-model"


@pytest.mark.asyncio
async def test_free_model_prose_json_accepted_for_json_mode() -> None:
    """A free model that can't do response_format returns prose-with-JSON.
    The dispatcher must accept it (lenient extract_json), not reject it."""
    d = _make_dispatcher()
    # free provider returns a leading sentence + a JSON object (no response_format)
    provider = _FakeProvider("openrouter", "ok")

    async def _fake_complete(model_id, request):
        return type("R", (), {"text": 'Here is the plan: {"agents": ["general"]}', "model_used": model_id})()

    provider.complete = _fake_complete
    d._registry[("free-model", "openrouter")] = (
        d._registry[("free-model", "openrouter")][0],
        provider,
    )
    req = CompletionRequest(
        prompt="plan my week", component="planner", priority=PoolPriority.HIGH, json_mode=True
    )
    resp = await d.complete(req)
    assert "general" in resp.text


@pytest.mark.asyncio
async def test_no_fallback_when_free_also_down() -> None:
    """If even the free fallback is exhausted, the original error surfaces."""
    from inference.exceptions import AllModelsRateLimitedError

    d = _make_dispatcher()
    # Make the free provider also "pay" so both pools fail.
    d._registry[("free-model", "openrouter")] = (
        d._registry[("free-model", "openrouter")][0],
        _FakeProvider("openrouter", "pay"),
    )
    req = CompletionRequest(prompt="hi", component="planner", priority=PoolPriority.HIGH)
    with pytest.raises(AllModelsRateLimitedError):
        await d.complete(req)


@pytest.mark.asyncio
async def test_free_fallback_not_duplicated_when_primary_empty() -> None:
    """When primary candidates list is empty, free fallback should be called once, not twice."""
    from inference.exceptions import AllModelsRateLimitedError

    d = _make_dispatcher()
    # Remove paid model so primary candidates are empty
    del d._registry[("paid-model", "opencode_zen")]

    call_count = 0
    provider = _FakeProvider("openrouter", "pay")

    async def _failing_complete(model_id, request):
        nonlocal call_count
        call_count += 1
        raise PaymentRequiredError(model_id, "openrouter")

    provider.complete = _failing_complete
    d._registry[("free-model", "openrouter")] = (
        d._registry[("free-model", "openrouter")][0],
        provider,
    )

    req = CompletionRequest(prompt="hi", component="planner", priority=PoolPriority.HIGH)
    with pytest.raises(AllModelsRateLimitedError):
        await d.complete(req)

    assert call_count == 1

