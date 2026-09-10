"""Read a weekday selection however a caller chose to say it.

This is pure vocabulary: it turns a day number, a day name, a group word, or a
list mixing any of those into a canonical ``frozenset`` of 0=Mon..6=Sun (or
``None`` for every day). It belongs to the platform layer so a schedule tool and
an HTTP route can share one reading of "every weekday" without either reaching
across into the other's package.

The parse results and the ValueError sentences here are a contract: a model that
sends a bad day gets a message it can act on, and the four schedule tools plus
`north cron` all agree on what the words mean.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast

# A weekday is stored as 0=Mon..6=Sun. These groups name the common cuts.
WEEKDAYS = frozenset({0, 1, 2, 3, 4})
WEEKENDS = frozenset({5, 6})
_EVERY_DAY = frozenset(range(7))


# What a person calls each day, and the shorthands they use for groups of them.
# Read leniently on purpose: a model asked for "every weekday" has a dozen
# reasonable ways to say so, and every one it picks that north rejects turns a
# working feature into an error message.
_DAY_WORDS: dict[str, int] = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "weds": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}
_DAY_GROUPS: dict[str, frozenset[int] | None] = {
    "weekday": WEEKDAYS,
    "weekdays": WEEKDAYS,
    "workday": WEEKDAYS,
    "workdays": WEEKDAYS,
    "weekend": WEEKENDS,
    "weekends": WEEKENDS,
    "daily": None,
    "everyday": None,
    "every day": None,
    "all": None,
}


def normalise_weekdays(value: object) -> frozenset[int] | None:
    """Coerce a stored or caller-supplied weekday selection to the canonical form.

    ``None`` means every day, and so does any selection that names all seven -
    "Mon through Sun" and "daily" are the same rule, and keeping two spellings of
    it would let ``describe()`` say "every Mon, Tue, Wed, Thu, Fri, Sat, Sun".
    A lone integer is accepted because that is what single-day schedules were
    stored as before this field held a set.
    """
    if value is None:
        return None
    iterable = cast(Iterable[object], value)
    days = frozenset({int(value)}) if isinstance(value, int) else frozenset(int(cast(Any, day)) for day in iterable)
    if not days:
        return None
    for day in days:
        if not 0 <= day <= 6:
            raise ValueError(f"weekday must be in [0, 6], got {day}")
    return None if days == _EVERY_DAY else days


def parse_weekdays(value: object) -> frozenset[int] | None:
    """Read a weekday selection however the caller chose to say it.

    Accepts a day number, a day name, one of the group words ("weekdays",
    "weekends", "daily"), or a list mixing any of those. Returns None for a
    schedule that runs every day.

    Raises ValueError with a sentence a model can act on. The old code called
    ``int()`` on whatever arrived, so "every weekday" - which reaches a tool as
    a list - failed with "int() argument must be ... not 'list'", which tells
    the reader nothing about days of the week.
    """
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in _DAY_GROUPS:
        return _DAY_GROUPS[value.strip().lower()]
    items = value if isinstance(value, list | tuple | set | frozenset) else [value]
    days: set[int] = set()
    for item in items:
        days.update(_one_selection(item))
    return normalise_weekdays(days)


def _one_selection(item: object) -> frozenset[int]:
    """The days named by a single element of a weekday selection."""
    if isinstance(item, bool):  # bool is an int; a True here is a caller error
        raise ValueError(f"{item!r} is not a day of the week")
    if isinstance(item, int):
        if not 0 <= item <= 6:
            raise ValueError(f"day must be 0 (Monday) to 6 (Sunday), got {item}")
        return frozenset({item})
    word = str(item).strip().lower()
    if word in _DAY_WORDS:
        return frozenset({_DAY_WORDS[word]})
    if word in _DAY_GROUPS:
        group = _DAY_GROUPS[word]
        return frozenset(range(7)) if group is None else group
    if word.isdigit():
        return _one_selection(int(word))
    raise ValueError(
        f"{item!r} is not a day of the week. Use 0-6 (Monday to Sunday), a day name "
        f"like 'Tue', or one of: weekdays, weekends, daily."
    )
