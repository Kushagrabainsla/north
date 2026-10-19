"""Every live test runs under a time limit, so one that hangs fails by name instead of stalling the nightly check."""

from __future__ import annotations

import pytest

from tests.live import conftest as live


class _Item:
    def __init__(self, nodeid: str, marker: pytest.Mark | None = None) -> None:
        self.nodeid = nodeid
        self.markers = [marker] if marker else []

    def get_closest_marker(self, name: str) -> pytest.Mark | None:
        return next((mark for mark in self.markers if mark.name == name), None)

    def add_marker(self, marker: pytest.MarkDecorator) -> None:
        self.markers.append(marker.mark)


def test_a_live_test_gets_ten_minutes_twice_what_a_throttled_codex_needs() -> None:
    """One waited 14 days on an agent that never answered; Codex, rate-limited, waits about 278 s."""
    item = _Item("tests/live/test_ask_north_live.py::test_in_autonomous_mode_north_answers_the_agent_from_memory")

    live.pytest_collection_modifyitems(None, [item])  # type: ignore[arg-type]

    assert item.get_closest_marker("timeout").args == (600,)


def test_a_test_with_its_own_limit_and_tests_elsewhere_are_left_alone() -> None:
    own = _Item("tests/live/test_x.py::test_slow", pytest.mark.timeout(900).mark)
    unit = _Item("tests/unit/test_x.py::test_fast")

    live.pytest_collection_modifyitems(None, [own, unit])  # type: ignore[arg-type]

    assert own.get_closest_marker("timeout").args == (900,)
    assert unit.get_closest_marker("timeout") is None
