"""Issue #87: what do meta tools cost a task: context, and the prompt cache? (counts only)

Reads ~/.north/tasks/tasks.db read-only.

1. Context: for each run that called create_tool, create_skill or create_flow, the share
   of all tool-result characters that came from those calls.
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
META = {"create_tool", "create_skill", "create_flow"}
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


if __name__ == "__main__":
    main()
