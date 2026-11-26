"""Issue #87: did the main runs pass the skill check by changing their own example requests? (counts only)

For each run whose create_skill drafts failed the selection check, compares the example
requests of the first failed draft with those of the draft that passed: how many
positive and negative requests were kept, dropped or added.

Run: .venv/bin/python -m experiments.context_learning.e87_prompt_changes
"""

from __future__ import annotations

from experiments.context_learning.e87_skill_check import drafts

SINCE = "2026-11-13"


def main() -> None:
    calls = [c for c in drafts() if c["at"] >= SINCE]
    for run_id in dict.fromkeys(c["run_id"] for c in calls if c["selection_failed"]):
        run = [c for c in calls if c["run_id"] == run_id]
        first = next(c for c in run if c["selection_failed"])
        final = next((c for c in reversed(run) if c["passed"]), None)
        if final is None:
            print(f"{first['at']}: no passing draft recorded")
            continue
        line = [f"{first['at']}:"]
        for kind in ("positive_prompts", "negative_prompts"):
            before = {str(p) for p in first["params"].get(kind) or []}
            after = {str(p) for p in final["params"].get(kind) or []}
            kept, dropped, added = len(before & after), len(before - after), len(after - before)
            line.append(f"{kind.split('_')[0]} kept {kept}, dropped {dropped}, added {added}")
        changed = first["params"].get("description") != final["params"].get("description")
        line.append(f"description changed: {changed}")
        print("  ".join(line))


if __name__ == "__main__":
    main()
