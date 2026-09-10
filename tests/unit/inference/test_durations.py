"""One parser now serves both the rate-limit reader and the provider error path.

The two copies were byte-identical, so these cases pin the behavior both call
sites relied on.
"""

from __future__ import annotations

import pytest

from inference.durations import parse_duration_seconds


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("12s", 12.0),
        ("0.5s", 0.5),
        ("1500ms", 1.5),
        ("  7s  ", 7.0),
        ("30", 30.0),
        ("-5s", 0.0),
    ],
)
def test_durations_are_read_in_seconds(given: str, expected: float) -> None:
    assert parse_duration_seconds(given) == expected


@pytest.mark.parametrize("given", ["", "   ", "soon", "abcms", None])
def test_unusable_values_report_no_signal(given: str | None) -> None:
    assert parse_duration_seconds(given) is None  # type: ignore[arg-type]


def test_both_call_sites_use_the_shared_parser() -> None:
    from inference import rate_limit_status
    from inference.providers import openai_compat

    assert rate_limit_status._parse_duration_seconds is parse_duration_seconds
    assert openai_compat.parse_duration_seconds is parse_duration_seconds
