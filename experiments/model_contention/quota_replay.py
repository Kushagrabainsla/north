"""Issue #8: under a free-tier request limit, would task calls fail less if background work waited?

Replays the real timeline of routed calls (~/.north/models.db, read-only) against a
simulated limit of N calls a minute that all of north shares, as on a free tier.

- shared:  every call takes the next free place; a call over the limit is refused
- yield:   a background call waits while a task call happened in the last 60 s, and
           runs later; a task call over the limit is refused

Counts the task calls that would be refused. Background calls that wait are delayed,
never dropped; their extra delay is reported.

Run: .venv/bin/python -m experiments.model_contention.quota_replay [since]
"""

from __future__ import annotations

import sqlite3
import statistics
import sys
from collections import deque
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from config.strategy import WorkKind  # noqa: E402
from inference.routing.parts import kind_of  # noqa: E402

DB = Path.home() / ".north" / "models.db"
SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-11-13"
WINDOW = 60.0


def _t(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def replay(calls: list[tuple[float, bool]], limit: int, yielding: bool) -> tuple[int, int, list[float]]:
    """(task calls refused, task calls, background delays in seconds)."""
    used: deque[float] = deque()
    waiting: deque[float] = deque()
    last_task = -1e18
    refused = tasks = 0
    delays: list[float] = []

    def free(now: float) -> bool:
        while used and used[0] <= now - WINDOW:
            used.popleft()
        return len(used) < limit

    def drain(now: float) -> None:
        while waiting and now - last_task >= WINDOW and free(now):
            delays.append(now - waiting.popleft())
            used.append(now)

    for at, is_task in calls:
        if yielding:
            drain(at)
        if is_task:
            tasks += 1
            last_task = at
            if free(at):
                used.append(at)
            else:
                refused += 1
        elif yielding and (at - last_task < WINDOW or waiting):
            waiting.append(at)
        elif free(at):
            used.append(at)
    if yielding:
        end = calls[-1][0] if calls else 0.0
        step = end
        while waiting and step < end + 7 * 24 * 3600:
            step += 1.0
            drain(step)
    return refused, tasks, delays


def main() -> None:
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT part, created_at FROM routing_decisions WHERE created_at >= ? ORDER BY created_at", (SINCE,)
        ).fetchall()
    calls = [(_t(c), kind_of(p) is not WorkKind.BACKGROUND) for p, c in rows]
    background = sum(not task for _, task in calls)
    print(f"calls since {SINCE}: {len(calls)} (background {background})")
    for limit in (10, 20, 30):
        shared, tasks, _ = replay(calls, limit, yielding=False)
        yielded, _, delays = replay(calls, limit, yielding=True)
        median = statistics.median(delays) if delays else 0.0
        print(
            f"limit {limit}/min: task calls refused shared={shared} ({shared / tasks:.2%}) "
            f"yield={yielded} ({yielded / tasks:.2%}); background delayed {len(delays)}, median {median:.0f}s"
        )


if __name__ == "__main__":
    main()
