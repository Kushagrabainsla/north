"""Issue #39: what did the approval model cite when it decided for the user? (counts only)

Reads ~/.north/approvals.db read-only. For each card the memory decider decided,
counts the kinds of memory it cited, and how many approvals rest only on facts or
the profile (memory a model extracted), with no message, past decision or rule of
the user's behind them.

Run: .venv/bin/python -m experiments.approval_trust.cited_kinds
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

DB = Path.home() / ".north" / "approvals.db"
STATED = {"user_message", "past_decision", "judgement_rules"}
EXTRACTED = {"fact", "profile"}


def main() -> None:
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as conn:
        rows = [json.loads(p) for (p,) in conn.execute("SELECT payload FROM approval_cards")]
    decided = [r for r in rows if r.get("decided_by") == "memory_decider"]
    kinds: Counter[str] = Counter()
    only_extracted = no_cover = 0
    by_status: Counter[str] = Counter()
    sample_keys: Counter[str] = Counter()
    for card in decided:
        by_status[card["status"]] += 1
        cited = {m.get("kind", "") for m in card.get("memory_used") or []}
        kinds.update(cited)
        if card["status"] == "approved":
            if cited and cited <= EXTRACTED:
                only_extracted += 1
                sample_keys[(card.get("agent", ""), (card.get("action_label") or "")[:40].split(" ")[0])] += 1
            if not cited:
                no_cover += 1
    print(f"cards decided by the memory decider: {len(decided)} {dict(by_status)}")
    print(f"cited kinds (cards citing each): {dict(kinds)}")
    print(f"approvals that cite only facts or the profile: {only_extracted}")
    print(f"approvals that cite nothing: {no_cover}")
    print(f"agents of fact-only approvals: {dict(Counter(a for a, _ in sample_keys.elements()))}")


if __name__ == "__main__":
    main()
