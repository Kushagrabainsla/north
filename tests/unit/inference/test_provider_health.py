"""Tests for provider-level circuit breaking."""

from __future__ import annotations

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
