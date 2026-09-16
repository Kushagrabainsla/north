"""Prompt-cache reporting distinguishes measured misses from absent telemetry."""

from web.api import _cache_summary


def test_cache_summary_measures_warm_and_cold_turns() -> None:
    summary = _cache_summary(
        [
            {"actual_input_tokens": 1_000, "cached_tokens": 0, "cache_write_tokens": 1_000},
            {"actual_input_tokens": 1_200, "cached_tokens": 800, "cache_write_tokens": 0},
            {"actual_input_tokens": 1_400, "cached_tokens": 900, "cache_write_tokens": 0},
        ]
    )

    assert summary["telemetry"] == "reported"
    assert summary["warm_turns"] == 2
    assert summary["cold_turns"] == 1
    assert summary["cached_tokens"] == 1_700
    assert summary["uncached_input_tokens"] == 1_900
    assert summary["input_cache_ratio"] == 1_700 / 3_600


def test_cache_summary_does_not_call_unreported_counters_cold() -> None:
    summary = _cache_summary(
        [
            {"actual_input_tokens": 1_000, "cached_tokens": 0, "cache_write_tokens": 0},
            {"actual_input_tokens": 1_200, "cached_tokens": 0, "cache_write_tokens": 0},
        ]
    )

    assert summary["telemetry"] == "unreported"
    assert summary["cold_turns"] == 0
    assert summary["unknown_turns"] == 2
