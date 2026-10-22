"""Inside a run: which later calls lost cached prompt they could have reused, and why.

A later call must send fresh what was added since the call before it (tool
results, the model's last reply). Anything uncached beyond that is a miss: the
prompt changed ahead of the new part. Each miss is shown with whether the tool
list changed, since Codex caches the tools ahead of everything else.

No model is called. Reads prompt_profile events from ~/.north/tasks/tasks.db.

Run:
    .venv/bin/python -m experiments.codex_prompt_cache.misses --since 2026-10-17 --until 2026-10-22T19:10
"""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from config.settings import settings

# Uncached tokens over the new part that still count as a hit: Codex caches in
# 128-token blocks, and north's section sizes are estimates.
_SLACK_TOKENS = 2048


@dataclass(frozen=True)
class LaterCall:
    """One call after a run's first, with the call before it in the same run."""

    timestamp: str
    run_id: str
    tokens_in: int
    cached_tokens: int
    previous_tokens_in: int
    tools_changed: bool

    @property
    def new_tokens(self) -> int:
        """Prompt added since the previous call, which no cache can hold yet."""
        return max(self.tokens_in - self.previous_tokens_in, 0)

    @property
    def lost_tokens(self) -> int:
        """Uncached tokens beyond the new part."""
        return self.tokens_in - self.cached_tokens - self.new_tokens


def misses(calls: Sequence[LaterCall], slack: int = _SLACK_TOKENS) -> list[LaterCall]:
    """Calls that lost more cached prompt than block rounding explains."""
    return [call for call in calls if call.lost_tokens > slack]


def summarize(calls: Sequence[LaterCall]) -> dict[str, int]:
    """Totals for a set of later calls: what was sent, cached, new, and lost to misses."""
    missed = misses(calls)
    return {
        "calls": len(calls),
        "tokens_in": sum(call.tokens_in for call in calls),
        "cached": sum(call.cached_tokens for call in calls),
        "uncached": sum(call.tokens_in - call.cached_tokens for call in calls),
        "new": sum(call.new_tokens for call in calls),
        "misses": len(missed),
        "misses_with_tool_change": sum(call.tools_changed for call in missed),
        "lost_to_misses": sum(call.lost_tokens for call in missed),
    }


def load_later_calls(tasks_db: Path, since: str, until: str) -> list[LaterCall]:
    """Every general-agent call after a run's first, from prompt_profile events."""
    query = """
        WITH p AS (
            SELECT e.id, e.timestamp, e.run_id,
                   json_extract(e.data, '$.turn') AS turn,
                   json_extract(e.data, '$.actual_input_tokens') AS tokens_in,
                   json_extract(e.data, '$.cached_tokens') AS cached,
                   json_extract(e.data, '$.sections.tool_schemas') AS tools,
                   LAG(json_extract(e.data, '$.actual_input_tokens')) OVER w AS previous_in,
                   LAG(json_extract(e.data, '$.sections.tool_schemas')) OVER w AS previous_tools
            FROM agent_run_events e JOIN agent_runs r ON r.run_id = e.run_id
            WHERE e.event = 'prompt_profile' AND r.agent = 'general'
              AND e.timestamp >= ? AND e.timestamp < ?
            WINDOW w AS (PARTITION BY e.run_id ORDER BY e.id))
        SELECT timestamp, run_id, tokens_in, cached, previous_in, tools <> previous_tools
        FROM p WHERE turn > 1 AND previous_in IS NOT NULL ORDER BY id
    """
    with sqlite3.connect(tasks_db) as conn:
        rows = conn.execute(query, (since, until)).fetchall()
    return [
        LaterCall(ts, run, tin or 0, cached or 0, prev or 0, bool(changed))
        for ts, run, tin, cached, prev, changed in rows
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="2026-10-17")
    parser.add_argument("--until", default="9999")
    args = parser.parse_args()
    calls = load_later_calls(settings.north_home / "tasks" / "tasks.db", args.since, args.until)
    for key, value in summarize(calls).items():
        print(f"{key:<24} {value:,}")
    print("\nmisses: timestamp, run, tokens lost, tool list changed")
    for call in misses(calls):
        print(f"{call.timestamp[:19]}  {call.run_id[:8]}  {call.lost_tokens:>6,}  {call.tools_changed}")


if __name__ == "__main__":
    main()
