"""Issue #8: does north's background work still compete with the task the user asked for? (counts only)

Reads ~/.north/models.db (routing_decisions) read-only. Each row is one routed model
call: the part of north that made it, when, and how it ended. Parts are grouped into
the kinds of work of the Models panel (inference.routing.parts.kind_of).

For each foreground call that did not succeed, it counts the background calls in the
60 seconds before it. If background work starved the foreground, failed foreground
calls would follow background bursts more often than successful ones do.

Run: .venv/bin/python -m experiments.model_contention.contention
"""

from __future__ import annotations

import bisect
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from config.strategy import WorkKind  # noqa: E402
from inference.routing.parts import kind_of  # noqa: E402

DB = Path.home() / ".north" / "models.db"
WINDOW_SECONDS = 60
SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-11-01"


def _t(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def main() -> None:
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT part, outcome, created_at FROM routing_decisions WHERE created_at >= ? ORDER BY created_at",
            (SINCE,),
        ).fetchall()
    print(f"routed calls since {SINCE}: {len(rows)}")
    by_kind: dict[str, Counter[str]] = defaultdict(Counter)
    for part, outcome, _ in rows:
        by_kind[kind_of(part).value][outcome or "(none)"] += 1
    for kind, outcomes in sorted(by_kind.items()):
        total = sum(outcomes.values())
        print(f"  {kind:11s} {total:6d}  {dict(outcomes.most_common(5))}")
    parts = Counter(part for part, _, _ in rows if kind_of(part) is WorkKind.BACKGROUND)
    print(f"background parts: {dict(parts.most_common(8))}")

    background_times = sorted(_t(c) for p, _, c in rows if kind_of(p) is WorkKind.BACKGROUND)

    def recent_background(at: float) -> int:
        return bisect.bisect_left(background_times, at) - bisect.bisect_left(background_times, at - WINDOW_SECONDS)

    groups: dict[str, list[int]] = defaultdict(list)
    for part, outcome, created in rows:
        if kind_of(part) is not WorkKind.TASKS:
            continue
        ok = (outcome or "") in ("success", "ok", "served")
        groups["succeeded" if ok else "did not succeed"].append(recent_background(_t(created)))
    for name, values in groups.items():
        busy = sum(v > 0 for v in values)
        share = busy / max(len(values), 1)
        mean = statistics.mean(values or [0])
        print(
            f"foreground calls that {name}: {len(values)}; with background calls in the {WINDOW_SECONDS}s before: "
            f"{busy} ({share:.1%}); mean background calls then: {mean:.2f}"
        )


if __name__ == "__main__":
    main()
