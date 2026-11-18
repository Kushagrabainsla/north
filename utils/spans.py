"""Where the time of one tool call goes: named steps, timed, for the run's `tool_timing` event.

A browser call took 5 s and the saved events showed only its start and end: the page
check for the approval card, the approval decision and the action itself were one
number (issue #83). `collecting()` opens a list for one call; `span(name)` adds the
time of a step to it, wherever the step runs below the call. Outside a call, `span`
times nothing and costs nothing.

The record goes to the run's events, beside `tool_called` and `tool_result`: no new
log (CODING_STYLE §22).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_current: ContextVar[list[tuple[str, float]] | None] = ContextVar("north_spans", default=None)


@contextmanager
def span(name: str) -> Iterator[None]:
    """Time the step inside the block as *name*, when a call is collecting."""
    spans = _current.get()
    if spans is None:
        yield
        return
    start = time.perf_counter()
    try:
        yield
    finally:
        spans.append((name, (time.perf_counter() - start) * 1000))


@contextmanager
def collecting() -> Iterator[list[tuple[str, float]]]:
    """Collect the spans of the steps inside the block, in the order they end."""
    spans: list[tuple[str, float]] = []
    token = _current.set(spans)
    try:
        yield spans
    finally:
        _current.reset(token)


def totals(spans: list[tuple[str, float]]) -> dict[str, dict[str, float]]:
    """Each step's count and total milliseconds: a step that runs three times is one entry."""
    out: dict[str, dict[str, float]] = {}
    for name, ms in spans:
        entry = out.setdefault(name, {"n": 0, "ms": 0.0})
        entry["n"] += 1
        entry["ms"] += ms
    return {name: {"n": entry["n"], "ms": round(entry["ms"], 1)} for name, entry in out.items()}
