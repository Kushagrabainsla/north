"""Issue #90: how often does a fact change while a task that loaded it is running?

Read-only, counts only. Every task prompt carries the profile, which holds the facts, so every
run loads every current fact: a fact change inside a run's time window counts as a change to a
fact that run loaded. This is an upper bound.

A change is:
- a new fact (its observed_at), and
- a fact replaced or retired (status superseded: its updated_at, when it was retired).
Touches that only restate a fact are not changes of value and are not counted.

Prints counts for top-level runs (chat and scheduled tasks) and for all runs.
    .venv/bin/python experiments/memory_writes/e90_changes_during_runs.py
"""

from __future__ import annotations

import bisect
import json
import sqlite3
from datetime import datetime
from pathlib import Path

HOME = Path.home() / ".north"
OUT = Path(__file__).resolve().parent / "results"


def parse(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def main() -> None:
    with sqlite3.connect(f"file:{HOME / 'facts.db'}?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT observed_at, updated_at, status, content FROM context_facts").fetchall()
    events = sorted(
        (t, ("new: " if kind == "new" else "retired: ") + content[:120])
        for observed, updated, status, content in rows
        for t, kind in ([(parse(observed), "new")] + ([(parse(updated), "old")] if status == "superseded" else []))
        if t is not None
    )
    changes = [t for t, _ in events]
    with sqlite3.connect(f"file:{HOME / 'tasks' / 'tasks.db'}?mode=ro", uri=True) as conn:
        runs = conn.execute(
            "SELECT started_at, completed_at, parent_run_id IS NULL, substr(prompt, 1, 160), task_id "
            "FROM agent_runs WHERE completed_at IS NOT NULL"
        ).fetchall()
    report: dict[str, object] = {"fact_changes_total": len(changes)}
    cases = []
    for started, completed, top, prompt, task_id in runs:
        start, end = parse(started), parse(completed)
        if top and start is not None and end is not None and end >= start:
            lo, hi = bisect.bisect_right(changes, start), bisect.bisect_right(changes, end)
            if hi > lo:
                cases.append(
                    {
                        "task_id": task_id,
                        "minutes": round((end - start) / 60, 1),
                        "prompt": prompt,
                        "changes": [text for _, text in events[lo:hi]],
                    }
                )
    # The cases hold fact text: they stay in ~/.north, and the repo keeps counts only.
    private = HOME / "experiments" / "memory_writes"
    private.mkdir(parents=True, exist_ok=True)
    (private / "e90_cases.json").write_text(json.dumps(cases, indent=2) + "\n")
    report["cases"] = len(cases)
    report["cases_open_over_a_day"] = sum(c["minutes"] > 24 * 60 for c in cases)
    report["cases_changes"] = [len(c["changes"]) for c in cases]
    for scope in ("top_level", "all"):
        considered = hit = 0
        minutes = 0.0
        for started, completed, top, _, _ in runs:
            if scope == "top_level" and not top:
                continue
            start, end = parse(started), parse(completed)
            if start is None or end is None or end < start:
                continue
            considered += 1
            minutes += (end - start) / 60
            inside = bisect.bisect_right(changes, end) - bisect.bisect_right(changes, start)
            hit += inside > 0
        report[scope] = {
            "runs": considered,
            "runs_with_a_fact_change_inside": hit,
            "share": round(hit / considered, 4) if considered else None,
            "mean_run_minutes": round(minutes / considered, 2) if considered else None,
        }
    first = min(parse(row[0]) for row in runs if parse(row[0]))
    report["fact_changes_since_first_run"] = sum(t >= first for t in changes)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "e90_changes_during_runs.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
