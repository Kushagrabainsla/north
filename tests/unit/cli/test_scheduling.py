from __future__ import annotations

import pytest

from cli.scheduling import day_selection


def test_day_selection_normalizes_lists_and_group_words() -> None:
    seen: list[list[str] | str] = []

    def validate(value: list[str] | str) -> None:
        seen.append(value)

    assert day_selection(None, validate) is None
    assert day_selection(" mon, wed , ,fri ", validate) == ["mon", "wed", "fri"]
    assert day_selection("weekdays", validate) == "weekdays"
    assert seen == [["mon", "wed", "fri"], "weekdays"]


def test_day_selection_preserves_validator_error() -> None:
    with pytest.raises(ValueError, match="invalid day"):
        day_selection("wrong", lambda _: (_ for _ in ()).throw(ValueError("invalid day")))
