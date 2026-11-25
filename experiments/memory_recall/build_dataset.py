"""Build the replay set: north's past tasks, each with the memory recall gives it today.

One row per distinct prompt of a finished top-level run of the general agent
(named `north` since 2026-11-10). Each row keeps the prompt, the tool calls, the
final answer and the recalled items with their ids. Written to DATA_DIR/cases.jsonl.

Run: .venv/bin/python -m experiments.memory_recall.build_dataset
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

from experiments.memory_recall.common import DATA_DIR, SNAPSHOT_DIR, MemorySnapshot, tokens, write_jsonl

MAX_TOOL_CALLS = 40
MAX_PARAM_CHARS = 300


def _runs() -> list[dict[str, Any]]:
    with sqlite3.connect(SNAPSHOT_DIR / "tasks.db") as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT run_id, task_id, agent, prompt, output, started_at, workspace FROM agent_runs "
            "WHERE agent IN ('general', 'north') AND status = 'completed' AND parent_run_id IS NULL "
            "AND length(output) > 0 ORDER BY started_at"
        ).fetchall()
        runs: dict[str, dict[str, Any]] = {}
        for row in rows:
            # One row per distinct prompt; a later run of the same prompt replaces an earlier one,
            # so a daily flow step counts once, with its newest answer.
            runs[row["prompt"][:200]] = dict(row)
        out = sorted(runs.values(), key=lambda r: r["started_at"])
        for run in out:
            events = conn.execute(
                "SELECT data FROM agent_run_events WHERE run_id = ? AND event = 'tool_called' ORDER BY id",
                (run["run_id"],),
            ).fetchall()
            calls = []
            for (data,) in events[:MAX_TOOL_CALLS]:
                parsed = json.loads(data)
                params = json.dumps(parsed.get("params", {}), ensure_ascii=False)[:MAX_PARAM_CHARS]
                calls.append({"tool": parsed.get("tool", ""), "params": params})
            run["tool_calls"] = calls
    return out


async def main() -> None:
    memory = MemorySnapshot()
    runs = _runs()
    # cases.jsonl: recall over today's memory (round 1 of #85 and #86 use it).
    # cases_asof.jsonl: recall over only the memory that existed when the task ran. The first
    # set leaked: half the notes it loaded were written after their task, some about that task.
    await _write(memory, runs, "cases.jsonl", as_of=False)
    cases = await _write(memory, runs, "cases_asof.jsonl", as_of=True)
    await _check(memory, cases)


async def _write(memory: MemorySnapshot, runs: list[dict[str, Any]], name: str, as_of: bool) -> list[dict[str, Any]]:
    cases = []
    for run in runs:
        recalled = await memory.recall(run["prompt"], as_of=run["started_at"] if as_of else None)
        cases.append(
            {
                "run_id": run["run_id"],
                "started_at": run["started_at"],
                "kind": "flow" if run["prompt"].startswith("Execute flow") else "chat",
                "prompt": run["prompt"],
                "output": run["output"],
                "tool_calls": run["tool_calls"],
                "facts": recalled["facts"],
                "episodes": recalled["episodes"],
                "profile": recalled["profile"],
            }
        )
    write_jsonl(DATA_DIR / name, cases)
    return cases


async def _check(memory: MemorySnapshot, cases: list[dict[str, Any]]) -> None:
    # A check that the replay recall (without the time filter) is the production recall.
    same = 0
    for case in cases[:20]:
        production = await memory.fact_store.search(case["prompt"])
        replay = await memory.recall(case["prompt"])
        same += production == [f.text for f in replay["facts"]]
    profile_tokens = sum(tokens(p.text) for p in cases[0]["profile"]) if cases else 0
    chats = sum(c["kind"] == "chat" for c in cases)
    print(f"cases={len(cases)} chat={chats} flow={len(cases) - chats}")
    print(f"replay recall equals production fact search in {same}/20 cases")
    print(f"profile sections={len(cases[0]['profile']) if cases else 0} profile_tokens={profile_tokens}")


if __name__ == "__main__":
    asyncio.run(main())
