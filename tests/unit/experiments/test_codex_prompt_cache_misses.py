"""A later call is a miss only when it lost more than the part added since the call before."""

from experiments.codex_prompt_cache.misses import LaterCall, misses, summarize


def _call(tokens_in: int, cached: int, previous_in: int, *, tools_changed: bool = False) -> LaterCall:
    return LaterCall("2026-10-22T12:00:00", "run", tokens_in, cached, previous_in, tools_changed)


def test_a_call_that_reused_everything_but_its_new_part_is_not_a_miss() -> None:
    call = _call(tokens_in=12_000, cached=10_000, previous_in=10_000)

    assert call.lost_tokens == 0
    assert misses([call]) == []


def test_a_call_that_lost_its_old_prompt_is_a_miss_counted_without_its_new_part() -> None:
    call = _call(tokens_in=12_000, cached=0, previous_in=11_500, tools_changed=True)

    assert misses([call]) == [call]
    assert call.lost_tokens == 11_500


def test_block_rounding_under_the_slack_is_not_a_miss() -> None:
    assert misses([_call(tokens_in=12_000, cached=9_000, previous_in=10_000)]) == []


def test_summary_separates_new_tokens_from_tokens_lost_to_misses() -> None:
    calls = [
        _call(tokens_in=12_000, cached=10_000, previous_in=10_000),
        _call(tokens_in=14_000, cached=0, previous_in=12_000, tools_changed=True),
    ]

    assert summarize(calls) == {
        "calls": 2,
        "tokens_in": 26_000,
        "cached": 10_000,
        "uncached": 16_000,
        "new": 4_000,
        "misses": 1,
        "misses_with_tool_change": 1,
        "lost_to_misses": 12_000,
    }
