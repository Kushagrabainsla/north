"""Tests for provider-level circuit breaking."""

from __future__ import annotations

import pytest

from inference.provider_health import ProviderHealthTracker


def test_provider_mark_down_blocks_until_expiry(monkeypatch) -> None:
    tracker = ProviderHealthTracker(down_seconds=10)

    assert tracker.is_available("openencode") is True
    assert tracker.mark_down("openencode", "auth failure") == "down"
    assert tracker.is_available("openencode") is False

    monkeypatch.setattr("inference.provider_health.time.monotonic", lambda: 1_000.0)
    tracker._records["openencode"].unhealthy_until = 999.0
    assert tracker.is_available("openencode") is True


def test_provider_degrades_after_repeated_failures() -> None:
    tracker = ProviderHealthTracker(degraded_threshold=2, degraded_seconds=15, max_degraded_seconds=15)

    assert tracker.mark_degraded("openencode", "first failure") == "healthy"
    assert tracker.is_available("openencode") is True

    assert tracker.mark_degraded("openencode", "second failure") == "degraded"
    assert tracker.is_available("openencode") is False


def test_provider_success_clears_health_state() -> None:
    tracker = ProviderHealthTracker()
    tracker.mark_down("openencode", "auth failure")
    tracker.record_success("openencode")

    assert tracker.is_available("openencode") is True


@pytest.mark.asyncio
async def test_model_404_does_not_degrade_provider(tmp_path) -> None:
    """Individual model 404s or 400s must cool down only that model without degrading provider."""
    from inference.capability import ModelCapability, ModelInfo
    from inference.dispatcher import ModelDispatcher
    from inference.exceptions import ModelNotFoundError
    from inference.models import CompletionRequest, CompletionResponse, PoolPriority

    class MockProvider:
        name = "openrouter"

        def get_models(self) -> dict[str, ModelInfo]:
            return {
                "claude-opus-4": ModelInfo(
                    model_id="claude-opus-4",
                    provider_name="openrouter",
                    capabilities=frozenset({ModelCapability.COMPLETION}),
                    context_window=128000,
                    cost_per_token=0.015,
                    base_quality=0.96,
                ),
                "llama-3.1-8b": ModelInfo(
                    model_id="llama-3.1-8b",
                    provider_name="openrouter",
                    capabilities=frozenset({ModelCapability.COMPLETION}),
                    context_window=128000,
                    cost_per_token=0.0,
                    base_quality=0.32,
                ),
            }

        async def complete(self, model_id: str, request: CompletionRequest) -> CompletionResponse:
            if model_id == "claude-opus-4":
                raise ModelNotFoundError(model_id, self.name)
            return CompletionResponse(text="hello", model_used=model_id, tokens_in=10, tokens_out=10, cost_usd=0.0)

    disp = ModelDispatcher([MockProvider()], cooldowns_path=tmp_path / "cooldowns.json")

    # Provider should remain available and dispatch should reach working-model
    resp = await disp.complete(CompletionRequest(prompt="hi", priority=PoolPriority.HIGH, component="test"))
    assert resp.model_used == "llama-3.1-8b"
    assert resp.text == "hello"
    assert disp._provider_health.is_available("openrouter") is True
    # The failed model was placed in cooldown without taking down the provider
    assert disp._cooldowns.is_active(("claude-opus-4", "openrouter")) is True
