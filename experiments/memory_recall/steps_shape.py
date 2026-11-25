"""Print how many tool calls the replayed tasks have, and which tools (counts only).

Run: .venv/bin/python -m experiments.memory_recall.steps_shape
"""

from __future__ import annotations

from collections import Counter

from experiments.memory_recall.common import DATA_DIR, read_jsonl


def main() -> None:
    cases = read_jsonl(DATA_DIR / "cases.jsonl")
    sizes = Counter(min(len(c["tool_calls"]), 10) for c in cases)
    print("tool calls per task (10 = 10 or more):", dict(sorted(sizes.items())))
    tools = Counter(call["tool"] for c in cases for call in c["tool_calls"])
    print("tools:", tools.most_common(25))


if __name__ == "__main__":
    main()
