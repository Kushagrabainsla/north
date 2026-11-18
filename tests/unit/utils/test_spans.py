"""Timing the steps of one tool call (issue #83)."""

from __future__ import annotations

import asyncio

from utils.spans import collecting, span, totals


def test_a_span_outside_a_call_times_nothing() -> None:
    with span("alone"):
        pass
    with collecting() as spans:
        pass
    assert spans == []


async def test_spans_inside_a_call_are_collected_with_their_time() -> None:
    with collecting() as spans, span("outer"), span("inner"):
        await asyncio.sleep(0.01)
    assert [name for name, _ in spans] == ["inner", "outer"]
    assert all(ms >= 10 for _, ms in spans)


async def test_a_span_in_a_thread_or_task_below_the_call_is_collected() -> None:
    def in_thread() -> None:
        with span("thread"):
            pass

    async def in_task() -> None:
        with span("task"):
            pass

    with collecting() as spans:
        await asyncio.to_thread(in_thread)
        await asyncio.gather(in_task(), in_task())
    assert sorted(name for name, _ in spans) == ["task", "task", "thread"]


def test_totals_count_a_repeated_step_once_with_its_sum() -> None:
    assert totals([("bridge.navigate", 1.25), ("decide", 3.0), ("bridge.navigate", 2.0)]) == {
        "bridge.navigate": {"n": 2, "ms": 3.2},
        "decide": {"n": 1, "ms": 3.0},
    }


def test_two_calls_collect_apart() -> None:
    with collecting() as first, span("a"):
        pass
    with collecting() as second, span("b"):
        pass
    assert [name for name, _ in first] == ["a"]
    assert [name for name, _ in second] == ["b"]
