"""Unit tests for ModelDispatcher in-flight auto-wait on transient rate limits."""

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import patch

from inference.capability import ModelInfo
from inference.dispatcher import ModelDispatcher
from inference.exceptions import AllModelsRateLimitedError, ModelRateLimitedError
from inference.models import CompletionRequest, CompletionResponse, PoolPriority


class MockTransientRateLimitedProvider:
    def __init__(self, name: str, model_id: str, retry_after: float):
        self.name = name
        self.model_id = model_id
        self.retry_after = retry_after
        self.call_count = 0
        self._models = {
            model_id: ModelInfo(
                model_id=model_id,
                provider_name=name,
                capabilities=frozenset(["completion"]),
                context_window=128000,
                cost_per_token=0.0,
                base_quality=0.8,
            )
        }

    def get_models(self) -> dict[str, ModelInfo]:
        return dict(self._models)

    async def complete(self, model_id: str, request: CompletionRequest):
        self.call_count += 1
        if self.call_count == 1:
            raise ModelRateLimitedError(model_id, self.name, retry_after=self.retry_after)
        return CompletionResponse(
            text="recovered after wait",
            model_used=model_id,
            tokens_in=5,
            tokens_out=5,
            cost_usd=0.0,
        )


@pytest.mark.asyncio
async def test_dispatcher_auto_waits_for_short_rate_limit():
    provider = MockTransientRateLimitedProvider("openrouter", "stealth/ox-alpha", retry_after=0.1)
    dispatcher = ModelDispatcher([provider])
    req = CompletionRequest(prompt="hello", component="general", priority=PoolPriority.MEDIUM)

    # Dispatcher should catch the 0.1s rate limit, sleep briefly, and succeed on the second attempt
    resp = await dispatcher.complete(req)
    assert resp.text == "recovered after wait"
    assert provider.call_count == 2


@pytest.mark.asyncio
async def test_dispatcher_raises_with_retry_after_when_exceeding_wait_cap():
    provider = MockTransientRateLimitedProvider("openrouter", "stealth/ox-alpha", retry_after=60.0)
    dispatcher = ModelDispatcher([provider])
    req = CompletionRequest(prompt="hello", component="general", priority=PoolPriority.MEDIUM)

    # 60s exceeds the 30s in-flight wait threshold, so it should raise AllModelsRateLimitedError with retry_after attached
    with pytest.raises(AllModelsRateLimitedError) as exc_info:
        await dispatcher.complete(req)

    assert exc_info.value.retry_after is not None
    assert 55.0 <= exc_info.value.retry_after <= 60.0
