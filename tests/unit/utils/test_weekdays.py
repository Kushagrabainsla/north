"""The platform reading of a weekday selection.

These pin the parse results and the model-facing error sentences directly on
``utils.weekdays``, independent of any schedule tool. The schedule tools and the
HTTP cron route both defer to this vocabulary, so a change that alters a result
here changes what "every weekday" means everywhere at once - the tests exist to
make that change deliberate.
"""

from __future__ import annotations

import pytest

from utils.weekdays import (
    WEEKDAYS,
    WEEKENDS,
    normalise_weekdays,
    parse_weekdays,
)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("weekdays", frozenset({0, 1, 2, 3, 4})),
        ("Weekends", frozenset({5, 6})),
        ("daily", None),
        ("everyday", None),
        ("every day", None),
        ("all", None),
        ("workdays", frozenset({0, 1, 2, 3, 4})),
        ([0, 1, 2, 3, 4], frozenset({0, 1, 2, 3, 4})),
        (["mon", "thu"], frozenset({0, 3})),
        (["Monday", "Thursday"], frozenset({0, 3})),
        (("tue", "weds"), frozenset({1, 2})),
        ({5, 6}, frozenset({5, 6})),
        ("tue", frozenset({1})),
        (2, frozenset({2})),
        (["1", "3"], frozenset({1, 3})),
        (None, None),
        ([0, 1, 2, 3, 4, 5, 6], None),
        (["weekdays", "weekends"], None),
    ],
)
def test_a_day_selection_is_read_however_it_is_said(given, expected) -> None:
    assert parse_weekdays(given) == expected


def test_group_constants_are_the_expected_days() -> None:
    assert frozenset({0, 1, 2, 3, 4}) == WEEKDAYS
    assert frozenset({5, 6}) == WEEKENDS


def test_an_unknown_day_word_says_what_to_send_instead() -> None:
    with pytest.raises(ValueError, match="not a day of the week"):
        parse_weekdays(["mon", "someday"])


def test_the_error_names_days_rather_than_python_types() -> None:
    """The old failure was "int() argument must be ... not 'list'"."""
    with pytest.raises(ValueError) as exc:
        parse_weekdays("often")
    assert "int()" not in str(exc.value)
    assert "weekdays" in str(exc.value)


def test_an_out_of_range_day_number_names_the_range() -> None:
    with pytest.raises(ValueError, match="day must be 0 \\(Monday\\) to 6 \\(Sunday\\), got 9"):
        parse_weekdays(9)


def test_a_boolean_is_a_caller_error_not_a_day() -> None:
    with pytest.raises(ValueError, match="is not a day of the week"):
        parse_weekdays([True])


def test_normalise_collapses_all_seven_to_daily() -> None:
    assert normalise_weekdays({0, 1, 2, 3, 4, 5, 6}) is None
    assert normalise_weekdays(3) == frozenset({3})
    assert normalise_weekdays(None) is None


def test_normalise_rejects_an_out_of_range_day() -> None:
    with pytest.raises(ValueError, match="weekday must be in \\[0, 6\\], got 7"):
        normalise_weekdays({7})
