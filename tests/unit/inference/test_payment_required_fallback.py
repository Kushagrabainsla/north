"""Unit test verifying that PaymentRequiredError on a paid model does not mark the entire provider down."""

from __future__ import annotations

import pytest

from inference.exceptions import PaymentRequiredError
from inference.capability import ModelInfo
from inference.models import CompletionRequest, CompletionResponse, PoolPriority
from inference.dispatcher import ModelDispatcher


class MockProvider:
    def __init__(self, name: str, models: dict[str, ModelInfo]):
        self.name = name
        self._models = models

    def get_models(self) -> dict[str, ModelInfo]:
        return dict(self._models)

    async def complete(self, model_id: str, request: CompletionRequest):
        if "paid" in model_id:
            raise PaymentRequiredError("0 credits")
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
