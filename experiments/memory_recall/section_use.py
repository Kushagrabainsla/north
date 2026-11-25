"""Count, per profile section, how often the #85 reference marked it used (counts only).

Run: .venv/bin/python -m experiments.memory_recall.section_use
"""

from __future__ import annotations

from collections import Counter

from experiments.memory_recall.common import DATA_DIR, read_jsonl


def main() -> None:
    rows = read_jsonl(DATA_DIR / "e85_labels.jsonl")
    cases = {c["run_id"]: c for c in read_jsonl(DATA_DIR / "cases.jsonl")}
    used: Counter[str] = Counter()
    by_kind: Counter[tuple[str, str]] = Counter()
    for row in rows:
        kind = cases[row["run_id"]]["kind"]
        for item_id, value in row["reference"].items():
            if value and item_id.startswith("profile:"):
                used[item_id] += 1
                by_kind[(item_id, kind)] += 1
    total = len(rows)
    for item_id, count in used.most_common():
        chat, flow = by_kind[(item_id, "chat")], by_kind[(item_id, "flow")]
        print(f"{item_id:40s} used in {count:3d}/{total} tasks  chat={chat} flow={flow}")
    profile_tasks = sum(any(v and k.startswith("profile:") for k, v in r["reference"].items()) for r in rows)
    print(f"tasks using any profile section: {profile_tasks}/{total}")


if __name__ == "__main__":
    main()
