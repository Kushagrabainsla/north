"""An update asked for "when idle" starts only after ten quiet minutes with nothing due (issue #84).

On 2026-11-17 restarts cut one outreach task off six times; it cost five times a normal run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.idle_update import IdleUpdate
from utils.update_record import last_update, record_update

WINDOW = 600.0


class _North:
    """What the watcher looks at, set by each test: the time, what runs, what is due."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.busy = ""
        self.due = ""
        self.started_task_at: float | None = None
        self.updates = 0

    async def is_busy(self) -> str:
        return self.busy

    async def due_soon(self, within: float) -> str:
        assert within == WINDOW
        return self.due


@pytest.fixture
def north() -> _North:
    return _North()


def _watcher(home: Path, north: _North) -> IdleUpdate:
    def start() -> None:
        north.updates += 1
        record_update(home, "running")

    return IdleUpdate(
        home,
        busy=north.is_busy,
        due_soon=north.due_soon,
        last_started=lambda: north.started_task_at,
        start=start,
        window=WINDOW,
        clock=lambda: north.now,
    )


async def test_nothing_happens_when_no_update_waits(tmp_path, north) -> None:
    assert await _watcher(tmp_path, north).check() == ""
    assert north.updates == 0


async def test_it_updates_after_ten_quiet_minutes_and_only_then(tmp_path, north) -> None:
    record_update(tmp_path, "waiting")
    watcher = _watcher(tmp_path, north)

    first = await watcher.check()
    assert "updates in about 10 min" in first
    assert "updates in about 10 min" in last_update(tmp_path)["detail"]  # what the System page shows
    north.now += WINDOW - 1
    assert north.updates == 0 and "about 1 min" in await watcher.check()

    north.now += 1
    assert await watcher.check() == "started"
    assert north.updates == 1
    assert await watcher.check() == ""  # it is running now, not waiting


async def test_something_running_restarts_the_ten_minutes(tmp_path, north) -> None:
    record_update(tmp_path, "waiting")
    watcher = _watcher(tmp_path, north)
    await watcher.check()

    north.now += 300
    north.busy = "1 task(s) running"
    assert await watcher.check() == "Waiting: 1 task(s) running."
    assert last_update(tmp_path)["detail"] == "Waiting: 1 task(s) running."

    north.busy = ""
    north.now += WINDOW - 1
    await watcher.check()
    assert north.updates == 0
    north.now += 1
    assert await watcher.check() == "started"


async def test_a_task_that_started_and_ended_between_two_looks_counts(tmp_path, north) -> None:
    record_update(tmp_path, "waiting")
    watcher = _watcher(tmp_path, north)
    await watcher.check()

    north.now += WINDOW
    north.started_task_at = north.now - 60  # a short chat a minute ago, done before this look
    await watcher.check()

    assert north.updates == 0
    north.now += 540
    assert await watcher.check() == "started"


async def test_work_due_in_the_next_ten_minutes_holds_it_back(tmp_path, north) -> None:
    record_update(tmp_path, "waiting")
    watcher = _watcher(tmp_path, north)
    await watcher.check()

    north.now += WINDOW
    north.due = "the Outreach schedule runs at Nov 19, 9:00 AM"
    assert await watcher.check() == "Waiting: the Outreach schedule runs at Nov 19, 9:00 AM."
    assert north.updates == 0

    north.due = ""
    assert await watcher.check() == "started"


async def test_a_cancelled_update_is_not_started(tmp_path, north) -> None:
    record_update(tmp_path, "waiting")
    watcher = _watcher(tmp_path, north)
    await watcher.check()
    record_update(tmp_path, "cancelled", "You cancelled the update before it started.")

    north.now += WINDOW * 2
    assert await watcher.check() == ""
    assert north.updates == 0
