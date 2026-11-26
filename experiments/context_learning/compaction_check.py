"""Issue #92: does history compaction run at all? (counts only)

Reads ~/.north/tasks/tasks.db and ~/.north/models.db read-only. Compaction
(agents/context_compaction.py) starts when a prompt reaches 75% of the model's context
window. This counts the model turns, the largest prompt, the turns above 100k and 150k
tokens (75% of a 200k window), and the compaction and tool-summary calls by month.

Run: .venv/bin/python -m experiments.context_learning.compaction_check [since]
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

HOME = Path.home() / ".north"
SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-11-01"


def main() -> None:
    with sqlite3.connect(f"file:{HOME / 'tasks' / 'tasks.db'}?mode=ro", uri=True) as conn:
        turns, largest, over_100k, over_150k = conn.execute(
            "SELECT COUNT(*), MAX(json_extract(data, '$.actual_input_tokens')), "
            "SUM(json_extract(data, '$.actual_input_tokens') > 100000), "
            "SUM(json_extract(data, '$.actual_input_tokens') > 150000) "
            "FROM agent_run_events WHERE event = 'prompt_profile' AND timestamp >= ?",
            (SINCE,),
        ).fetchone()
    print(f"model turns since {SINCE}: {turns}; largest prompt: {largest} tokens")
    print(f"turns over 100k tokens: {over_100k}; over 150k (75% of a 200k window): {over_150k}")
    with sqlite3.connect(f"file:{HOME / 'models.db'}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT substr(created_at, 1, 7), part, COUNT(*) FROM routing_decisions "
            "WHERE part LIKE '%:compact' OR part LIKE '%:tool-summary' GROUP BY 1, 2 ORDER BY 1"
        ).fetchall()
    print("compaction and tool-summary calls by month:")
    for month, part, count in rows:
        print(f"  {month}  {part:28s} {count}")
    if not any(part.endswith(":compact") for _, part, _ in rows):
        print("  no ':compact' call was ever recorded")


if __name__ == "__main__":
    main()
