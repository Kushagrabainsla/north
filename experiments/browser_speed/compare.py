"""Compare replay results of two north versions: time and tokens per task, median of the runs.

    .venv/bin/python experiments/browser_speed/compare.py BEFORE_DIR AFTER_DIR

Each folder is one `run.py` result (results.json and tasks/tasks.db). Tokens count every model call
of a task, the memory decider's included; time is the task's wall time and its browser calls.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def _ts(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


def _task_numbers(db: sqlite3.Connection, task_id: str, seconds: float) -> dict[str, float]:
    rows = db.execute("SELECT event, data FROM agent_run_events WHERE task_id=? ORDER BY id", (task_id,))
    events = sorted(((event, json.loads(data)) for event, data in rows), key=lambda item: item[1]["timestamp"])
    numbers: dict[str, float] = defaultdict(float)
    numbers["wall_s"] = seconds
    called: list[float] = []
    for event, data in events:
        if event == "inference_call":
            numbers["tokens_in"] += int(data.get("tokens_in") or 0)
            numbers["tokens_out"] += int(data.get("tokens_out") or 0)
            numbers["cost_usd"] += float(data.get("cost_usd") or 0)
            if data.get("category") == "agent":
                numbers["turns"] += 1
            else:
                numbers["other_model_calls"] += 1
        elif event == "tool_called" and data.get("tool") == "browser":
            called.append(_ts(data["timestamp"]))
        elif event == "tool_result" and data.get("tool") == "browser" and called:
            numbers["browser_s"] += _ts(data["timestamp"]) - called.pop(0)
            numbers["browser_calls"] += 1
    # Per turn and per call: the model picks how many turns a task takes, so totals mix the code with its choices.
    numbers["tokens_in_per_turn"] = numbers["tokens_in"] / max(numbers["turns"], 1)
    numbers["browser_s_per_call"] = numbers["browser_s"] / max(numbers["browser_calls"], 1)
    return numbers


def _version(folder: Path) -> dict[str, list[dict[str, float]]]:
    results = json.loads((folder / "results.json").read_text())
    db = sqlite3.connect(folder / "tasks" / "tasks.db")
    by_task: dict[str, list[dict[str, float]]] = defaultdict(list)
    for result in results:
        if result["status"] == "completed":
            by_task[result["task"]].append(_task_numbers(db, result["task_id"], result["seconds"]))
    return by_task


_COLUMNS = (
    "wall_s",
    "browser_s",
    "turns",
    "browser_calls",
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "tokens_in_per_turn",
    "browser_s_per_call",
)


def main(argv: list[str]) -> None:
    before, after = _version(Path(argv[0])), _version(Path(argv[1]))
    for task in sorted(set(before) | set(after)):
        print(f"\n=== {task}  (runs: before {len(before.get(task, []))}, after {len(after.get(task, []))})")
        print(f"{'median':<15}{'before':>12}{'after':>12}{'change':>9}")
        for column in _COLUMNS:
            old = statistics.median(run[column] for run in before.get(task, [{column: 0}]))
            new = statistics.median(run[column] for run in after.get(task, [{column: 0}]))
            change = f"{(new - old) / old:+.0%}" if old else ""
            fmt = (
                "{:>12,.3f}" if column == "cost_usd" else "{:>12,.0f}" if column.startswith("tokens") else "{:>12,.1f}"
            )
            print(f"{column:<15}{fmt.format(old)}{fmt.format(new)}{change:>9}")
        for label, runs in (("before", before.get(task, [])), ("after", after.get(task, []))):
            walls = ", ".join(f"{run['wall_s']:.0f}s/{run['tokens_in'] / 1000:.0f}k" for run in runs)
            print(f"  {label} runs (wall/input tokens): {walls}")


if __name__ == "__main__":
    main(sys.argv[1:])
