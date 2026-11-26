"""Check that the before and after runs of e88 stored and retired the same facts, call by call.

.venv/bin/python experiments/memory_writes/compare_e88.py
"""

import json
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"
before = {r["fact"]: r for r in json.loads((RESULTS / "e88_before.json").read_text())["rows"]}
after = {r["fact"]: r for r in json.loads((RESULTS / "e88_after.json").read_text())["rows"]}
assert before.keys() == after.keys()
same = sum(before[k]["retired"] == after[k]["retired"] and before[k]["success"] == after[k]["success"] for k in before)
print(f"same outcome: {same} of {len(before)} calls")
