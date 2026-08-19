"""Unit test verifying that PaymentRequiredError on a paid model does not mark the entire provider down."""

from __future__ import annotations

import pytest

from inference.capability import ModelInfo
from inference.dispatcher import ModelDispatcher
from inference.exceptions import AllModelsRateLimitedError, InferenceError, PaymentRequiredError
from inference.models import CompletionRequest, CompletionResponse, PoolPriority


class MockProvider:
    def __init__(self, name: str, models: dict[str, ModelInfo]):
        self.name = name
        self._models = models

    def get_models(self) -> dict[str, ModelInfo]:
        return dict(self._models)

    async def complete(self, model_id: str, request: CompletionRequest):
        if "paid" in model_id:
            raise PaymentRequiredError(model_id, self.name)
        return CompletionResponse(text="free success", model_used=model_id, tokens_in=1, tokens_out=1, cost_usd=0.0)


@pytest.mark.asyncio
async def test_payment_required_does_not_mark_provider_down():
    paid_info = ModelInfo(
        model_id="openrouter/paid-model",
        provider_name="openrouter",
        capabilities=frozenset(["completion"]),
        context_window=128000,
        cost_per_token=0.01,
        base_quality=0.9,
    )
    free_info = ModelInfo(
        model_id="openrouter/free-model",
        provider_name="openrouter",
        capabilities=frozenset(["completion"]),
        context_window=128000,
        cost_per_token=0.0,
        base_quality=0.35,
    )
    provider = MockProvider("openrouter", {
        "openrouter/paid-model": paid_info,
        "openrouter/free-model": free_info,
    })

    dispatcher = ModelDispatcher([provider])
    req = CompletionRequest(prompt="test", component="test", priority=PoolPriority.HIGH)
    resp = await dispatcher.complete(req)
    assert resp.text == "free success"
    assert resp.model_used == "openrouter/free-model"
    # Ensure openrouter provider is STILL healthy/available
    assert dispatcher._provider_health.is_available("openrouter") is True


@pytest.mark.asyncio
async def test_generic_inference_error_recorded_as_status_error():
    """A generic InferenceError (5xx, timeout, bad JSON, etc.) must surface in the
    status store as kind 'error' - not be invisible as a silent "all available".
    TranscriptionError subclasses InferenceError, so it is covered by the same branch.
    """
    info = ModelInfo(
        model_id="openrouter/flaky-model",
        provider_name="openrouter",
        capabilities=frozenset(["completion"]),
        context_window=128000,
        cost_per_token=0.0,
        base_quality=0.5,
    )

    class FlakyProvider:
        name = "openrouter"

        def get_models(self):
            return {"openrouter/flaky-model": info}

        async def complete(self, model_id: str, request: CompletionRequest):
            raise InferenceError("upstream 500 from provider")

    dispatcher = ModelDispatcher([FlakyProvider()])
    req = CompletionRequest(prompt="test", component="test", priority=PoolPriority.HIGH)
    with pytest.raises(AllModelsRateLimitedError):
        await dispatcher.complete(req)
    status = dispatcher.rate_limit_status()
    assert any(
        r["kind"] == "error" and r["model"] == "openrouter/flaky-model" for r in status
    )


@pytest.mark.asyncio
async def test_success_marks_model_checked():
    info = ModelInfo(
        model_id="openrouter/ok-model",
        provider_name="openrouter",
        capabilities=frozenset(["completion"]),
        context_window=128000,
        cost_per_token=0.0,
        base_quality=0.5,
    )

    class OkProvider:
        name = "openrouter"

        def get_models(self):
            return {"openrouter/ok-model": info}

        async def complete(self, model_id: str, request: CompletionRequest):
            return CompletionResponse(text="ok", model_used=model_id, tokens_in=1, tokens_out=1, cost_usd=0.0)

    dispatcher = ModelDispatcher([OkProvider()])
    req = CompletionRequest(prompt="test", component="test", priority=PoolPriority.HIGH)
    await dispatcher.complete(req)
    assert dispatcher._rate_limit_status.checked_count() == 1
