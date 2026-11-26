"""Issue #87: what do meta tools cost a task: context, and the prompt cache? (counts only)

Reads ~/.north/tasks/tasks.db read-only.

1. Context: for each run that did meta-programming (any authoring tool: create_tool,
   create_skill, create_flow, create_view), the share of all tool-result characters that
   came from those calls, and the tokens the meta work carried: each meta call's input
   and result stay in the conversation, so they are re-sent on every later turn of the
   run. That carried amount, as a share of the run's input tokens, is what a separate
   child run (#87) would remove, less a short receipt. Also: failed meta calls, and how
   the run ended.
2. Cache: model turns come with a prompt_profile event (input tokens, cached tokens,
   tool-schema tokens). A turn whose tool list grew since the turn before is a "tool
   change" turn; its cached share is compared with the other turns of the same runs.
   The tool call just before the change says what grew the list.

Run: .venv/bin/python -m experiments.context_learning.meta_tools [since]
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

DB = Path.home() / ".north" / "tasks" / "tasks.db"
# Every authoring tool north has: tools, skills, flows (schedules are flows) and views.
META = {"create_tool", "create_skill", "create_flow", "create_view"}
CHARS_PER_TOKEN = 4
SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-11-01"


def main() -> None:
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT run_id, event, data FROM agent_run_events WHERE timestamp >= ? "
            "AND event IN ('tool_called', 'tool_result', 'prompt_profile') ORDER BY run_id, id",
            (SINCE,),
        ).fetchall()
    runs: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for run_id, event, data in rows:
        runs[run_id].append((event, json.loads(data)))

    shares = []
    change_cached, other_cached = [], []
    growers: Counter[str] = Counter()
    lost_tokens = 0
    all_uncached = all_input = 0
    for events in runs.values():
        meta_chars = all_chars = 0
        last_tool = ""
        last_schema = None
        for event, data in events:
            if event == "tool_called":
                last_tool = str(data.get("tool", ""))
            elif event == "tool_result":
                size = len(str(data.get("formatted") or data.get("error") or ""))
                all_chars += size
                if data.get("tool") in META:
                    meta_chars += size
            elif event == "prompt_profile":
                schema = (data.get("sections") or {}).get("tool_schemas", 0)
                actual = data.get("actual_input_tokens") or 0
                cached = data.get("cached_tokens") or 0
                if not actual:
                    continue
                all_input += actual
                all_uncached += max(0, actual - cached)
                share = cached / actual
                if last_schema is not None and schema > last_schema:
                    change_cached.append(share)
                    growers[last_tool or "(start)"] += 1
                    lost_tokens += max(0, actual - cached)
                elif last_schema is not None:
                    other_cached.append(share)
                last_schema = schema
        if any(data.get("tool") in META for event, data in events if event == "tool_called") and all_chars:
            shares.append(meta_chars / all_chars)

    print(f"runs since {SINCE}: {len(runs)}")
    if shares:
        shares.sort()
        print(
            f"runs with a meta-tool call: {len(shares)}; "
            f"meta share of tool output: median {statistics.median(shares):.1%}, "
            f"runs at 3.5% or more: {sum(s >= 0.035 for s in shares)}"
        )
    print(
        f"turns after the tool list grew: {len(change_cached)}; mean cached share "
        f"{statistics.mean(change_cached or [0]):.1%}; uncached tokens on them: {lost_tokens}"
    )
    print(f"other later turns: {len(other_cached)}; mean cached share {statistics.mean(other_cached or [0]):.1%}")
    print(
        f"all turns: input {all_input} tokens, uncached {all_uncached}; "
        f"tool-list growth caused {lost_tokens / max(all_uncached, 1):.1%} of the uncached tokens"
    )
    print(f"tool called just before the list grew: {dict(growers.most_common(8))}")
    carried_report(runs)


def carried_report(runs: dict[str, list[tuple[str, dict]]]) -> None:
    """For runs with meta calls: tokens the meta work re-sent on later turns, failures, and the end."""
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as conn:
        status = dict(conn.execute("SELECT run_id, status FROM agent_runs").fetchall())
    shares, carried_all, input_all, calls, failed = [], 0, 0, Counter(), 0
    endings: Counter[str] = Counter()
    other_inputs, meta_inputs = [], []
    for run_id, events in runs.items():
        turns = [i for i, (event, _) in enumerate(events) if event == "prompt_profile"]
        run_input = sum((d.get("actual_input_tokens") or 0) for e, d in events if e == "prompt_profile")
        meta_at = [
            (i, d) for i, (e, d) in enumerate(events) if e in ("tool_called", "tool_result") and d.get("tool") in META
        ]
        if not meta_at:
            if run_input:
                other_inputs.append(run_input)
            continue
        carried = 0
        for index, data in meta_at:
            size = len(json.dumps(data.get("params", ""))) + len(str(data.get("formatted") or data.get("error") or ""))
            later_turns = sum(1 for t in turns if t > index)
            carried += size // CHARS_PER_TOKEN * later_turns
            if data.get("tool") in META and "params" in data:
                calls[data["tool"]] += 1
            if data.get("success") is False:
                failed += 1
        endings[status.get(run_id, "?")] += 1
        meta_inputs.append(run_input)
        carried_all += carried
        input_all += run_input
        if run_input:
            shares.append(carried / run_input)
    if not shares:
        print("no runs with meta calls")
        return
    print(f"meta-programming runs: {len(shares)}; calls: {dict(calls)}; failed meta calls: {failed}")
    print(f"how those runs ended: {dict(endings)}")
    print(
        f"input tokens per run, median: meta runs {statistics.median(meta_inputs):.0f}, "
        f"other runs {statistics.median(other_inputs or [0]):.0f}"
    )
    print(
        f"tokens the meta work re-sent on later turns: {carried_all} of {input_all} input tokens "
        f"({carried_all / max(input_all, 1):.1%}); per run median {statistics.median(shares):.1%}, "
        f"max {max(shares):.1%}"
    )


if __name__ == "__main__":
    main()
