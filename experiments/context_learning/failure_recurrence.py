"""Issue #77: do tool failures recur, so that a refiner could learn from them? (counts only)

Reads ~/.north/tasks/tasks.db read-only. Each failed tool result (success false) is
reduced to a signature: the tool and the first line of its error, with numbers, ids,
paths and quoted text masked. A signature that recurs in several runs is something a
skill or flow fix could prevent; one that never recurs is not.

Each signature is also sorted by its likely cause:
- environment: a missing tool, an outage, a rate limit, a timeout, a refused approval
- agent: everything else (wrong input, wrong command, bad selector, ...)

Run: .venv/bin/python -m experiments.context_learning.failure_recurrence [since]
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

DB = Path.home() / ".north" / "tasks" / "tasks.db"
SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-11-01"
ENVIRONMENT = re.compile(
    r"not found|rate.?limit|timed? ?out|timeout|unavailable|connection|refused|denied|rejected|"
    r"not approved|cancel|network|503|502|429|no model|quota",
    re.IGNORECASE,
)


def signature(tool: str, error: str) -> str:
    line = (error or "(no error text)").strip().splitlines()[0] if (error or "").strip() else "(no error text)"
    line = re.sub(r"(['\"`]).*?\1", "<q>", line)
    line = re.sub(r"(/[\w.\-]+)+", "<path>", line)
    line = re.sub(r"[0-9a-f]{8,}", "<id>", line)
    line = re.sub(r"\d+", "<n>", line)
    return f"{tool}: {line[:90]}"


def main() -> None:
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT run_id, data, timestamp FROM agent_run_events WHERE event = 'tool_result' AND timestamp >= ?",
            (SINCE,),
        ).fetchall()
    last_week = max((t for _, _, t in rows), default="")[:10]
    from datetime import date, timedelta

    week_start = (date.fromisoformat(last_week) - timedelta(days=7)).isoformat() if last_week else ""
    when: dict[str, list[str]] = defaultdict(list)
    results = [(run, json.loads(data)) for run, data, _ in rows]
    for (_run, _data, stamp), (_, parsed) in zip(rows, results, strict=True):
        if parsed.get("success") is False:
            sig = signature(str(parsed.get("tool", "")), str(parsed.get("error") or parsed.get("formatted") or ""))
            when[sig].append(stamp[:10])
    failed = [(run, d) for run, d in results if d.get("success") is False]
    print(
        f"tool results since {SINCE}: {len(results)}; failed: {len(failed)} ({len(failed) / max(len(results), 1):.1%})"
    )
    runs_by_sig: dict[str, set[str]] = defaultdict(set)
    count_by_sig: Counter[str] = Counter()
    for run, data in failed:
        sig = signature(str(data.get("tool", "")), str(data.get("error") or data.get("formatted") or ""))
        runs_by_sig[sig].add(run)
        count_by_sig[sig] += 1
    recurring = {s for s, runs in runs_by_sig.items() if len(runs) >= 3}
    agent_recurring = {s for s in recurring if not ENVIRONMENT.search(s)}
    print(f"failure signatures: {len(runs_by_sig)}; in 3 or more runs: {len(recurring)}")
    print(
        f"failures covered by recurring signatures: {sum(count_by_sig[s] for s in recurring)}; "
        f"of those with an agent cause: {sum(count_by_sig[s] for s in agent_recurring)} "
        f"in {len(agent_recurring)} signatures"
    )
    # Upper bound for a refiner: a signature is flagged once it has failed in 3 runs; a perfect
    # fix right then would prevent every later failure with that signature.
    flagged_after = 0
    for sig in agent_recurring:
        runs_seen: list[str] = []
        for (run, _data, _stamp), (_, parsed) in zip(rows, results, strict=True):
            if parsed.get("success") is not False:
                continue
            if signature(str(parsed.get("tool", "")), str(parsed.get("error") or parsed.get("formatted") or "")) != sig:
                continue
            if len(set(runs_seen)) >= 3:
                flagged_after += 1
            runs_seen.append(run)
    print(
        f"failures after a signature's 3rd run (preventable at most, agent causes): {flagged_after} "
        f"of {len(failed)} failures ({flagged_after / max(len(failed), 1):.1%})"
    )
    still = {s for s in agent_recurring if any(d >= week_start for d in when[s])}
    print(
        f"agent-cause recurring signatures still seen in the last 7 days (from {week_start}): "
        f"{len(still)} of {len(agent_recurring)}, failures then: "
        f"{sum(sum(d >= week_start for d in when[s]) for s in still)}"
    )
    print("top recurring signatures (runs, failures, cause, first, last, last-7-days):")
    for sig in sorted(recurring, key=lambda s: -len(runs_by_sig[s]))[:15]:
        cause = "environment" if ENVIRONMENT.search(sig) else "agent"
        days = sorted(when[sig])
        recent = sum(d >= week_start for d in days)
        print(
            f"  {len(runs_by_sig[sig]):3d} {count_by_sig[sig]:4d}  {cause:11s} {days[0]} {days[-1]} {recent:3d}  {sig}"
        )


if __name__ == "__main__":
    main()
