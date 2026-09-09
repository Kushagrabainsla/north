"""Tests for conservative prompt-cache miss accounting."""

from __future__ import annotations

from inference.cache_stats import NOISE_FLOOR_TOKENS, CacheWasteTracker


def test_does_not_claim_misses_when_provider_never_reports_cache() -> None:
    tracker = CacheWasteTracker()
    tracker.add(tokens_in=8_000, cached_tokens=0, cache_write_tokens=0)
    tracker.add(tokens_in=9_000, cached_tokens=0, cache_write_tokens=0)
    assert tracker.totals.missed_tokens == 0
    assert tracker.totals.miss_count == 0


def test_counts_large_miss_after_cache_activity() -> None:
    tracker = CacheWasteTracker()
    # First turn wrote a prefix: this provider demonstrably reports cache usage.
    tracker.add(tokens_in=8_000, cached_tokens=0, cache_write_tokens=8_000)
    # Second turn re-sends the earlier prefix but cache read is zero.
    tracker.add(tokens_in=9_000, cached_tokens=0, cache_write_tokens=0)
    assert tracker.totals.missed_tokens == 8_000
    assert tracker.totals.miss_count == 1


def test_does_not_count_new_prompt_suffix_as_waste() -> None:
    tracker = CacheWasteTracker()
    tracker.add(tokens_in=8_000, cached_tokens=0, cache_write_tokens=8_000)
    # 8k old prefix reused; 6k new tool output cannot have been cached before.
    tracker.add(tokens_in=14_000, cached_tokens=8_000, cache_write_tokens=0)
    assert tracker.totals.missed_tokens == 0


def test_ignores_small_breakpoint_granularity_noise() -> None:
    tracker = CacheWasteTracker()
    tracker.add(tokens_in=20_000, cached_tokens=20_000, cache_write_tokens=0)
    tracker.add(tokens_in=20_000, cached_tokens=20_000 - NOISE_FLOOR_TOKENS, cache_write_tokens=0)
    assert tracker.totals.miss_count == 0
