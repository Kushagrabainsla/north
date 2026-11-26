"""Issue #70: why do general (north) agent runs end "failed"? (counts only)

Reads ~/.north/tasks/tasks.db read-only and groups failed runs by month and by
the start of their error text, with numbers and ids masked so alike errors group.

Run: .venv/bin/python -m experiments.approval_trust.failed_runs
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from pathlib import Path

DB = Path.home() / ".north" / "tasks" / "tasks.db"


def shape(error: str) -> str:
    text = re.sub(r"[0-9a-f]{8,}", "<id>", error or "(no error text)")
    text = re.sub(r"\d+(\.\d+)?", "<n>", text)
    return text.strip().splitlines()[0][:110] if text.strip() else "(no error text)"


def main() -> None:
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT r.run_id, substr(r.started_at,1,7), r.error, "
            "(SELECT COUNT(*) FROM agent_run_events e WHERE e.run_id=r.run_id AND e.event='tool_called') "
            "FROM agent_runs r WHERE r.agent IN ('general','north') AND r.status='failed'"
        ).fetchall()
    months = Counter(m for _, m, _, _ in rows)
    print(f"failed runs: {len(rows)} by month {dict(sorted(months.items()))}")
    no_tools = [r for r in rows if r[3] == 0]
    print(f"failed with no tool call: {len(no_tools)}")
    for month in sorted(months):
        print(f"\n== {month}")
        for text, count in Counter(shape(e) for _, m, e, _ in rows if m == month).most_common(6):
            print(f"{count:5d}  {text}")


if __name__ == "__main__":
    main()
