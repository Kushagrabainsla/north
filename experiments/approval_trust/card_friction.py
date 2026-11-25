"""Issue #74: which cards still wait for the user, from which flows, and for how long? (counts only)

Reads ~/.north/approvals.db and ~/.north/flow_runs.db read-only. A card is matched
to a flow by its task id (flow step tasks carry the flow run's task id).

Run: .venv/bin/python -m experiments.approval_trust.card_friction
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

HOME = Path.home() / ".north"


def _ro(name: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{HOME / name}?mode=ro", uri=True)


def _when(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def main() -> None:
    with _ro("flow_runs.db") as conn:
        flow_of = {task: name for task, name in conn.execute("SELECT task_id, flow_name FROM flow_runs") if task}
    with _ro("approvals.db") as conn:
        cards = [json.loads(p) for (p,) in conn.execute("SELECT payload FROM approval_cards")]
    recent = [c for c in cards if (c.get("created_at") or "") >= "2026-11-19"]
    by_who: Counter[str] = Counter(c.get("decided_by") or "(none)" for c in recent)
    print(f"cards since 2026-11-19: {len(recent)}; decided by: {dict(by_who)}")

    waits: dict[str, list[float]] = defaultdict(list)
    yours: Counter[str] = Counter()
    for card in recent:
        if card.get("decided_by") != "you":
            continue
        origin = flow_of.get(card.get("task_id", ""), "chat or other")
        yours[origin] += 1
        start, end = _when(card.get("created_at", "")), _when(card.get("decided_at", ""))
        if start and end:
            waits[origin].append((end - start).total_seconds() / 60)
    print("\ncards you answered, by origin (count, median wait in minutes):")
    for origin, count in yours.most_common():
        median = statistics.median(waits[origin]) if waits[origin] else float("nan")
        print(f"{count:4d}  {median:8.1f}  {origin}")
    kinds = Counter((c.get("type", ""), c.get("source", "") or "-") for c in recent if c.get("decided_by") == "you")
    print(f"\nkinds of cards you answered (type, source): {dict(kinds)}")


if __name__ == "__main__":
    main()
