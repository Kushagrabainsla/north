"""Save the #91 replay's fact pools and picks, so the product cutoff can be checked against them.

For the policy that was built (similarity + 0.1 * (use rate - 0.5)), replays the tasks in
time order as e91_fading does, and saves for each evaluated task: every candidate fact
that clears the 0.30 similarity floor (as production's SQL search does), its use score at
that moment, and the facts the replay picked. verify_before_after.py feeds the same pools
to the product cutoff and counts the tasks where both pick the same facts.

Run: .venv/bin/python -m experiments.memory_recall.dump_pools
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from experiments.memory_recall.common import DATA_DIR, MemorySnapshot, read_jsonl
from experiments.memory_recall.e91_fading import Stats, _when, candidates, production_cut

WEIGHT = 0.1


async def main() -> None:
    memory = MemorySnapshot()
    cases = sorted(read_jsonl(DATA_DIR / "cases_asof.jsonl"), key=lambda c: c["started_at"])
    labels = {r["run_id"]: r["needed"] for r in read_jsonl(DATA_DIR / "e91_labels.jsonl")}
    stats = Stats()
    pools = []
    for index, case in enumerate(cases):
        when = _when(case["started_at"]) or datetime.now()
        items = await candidates(memory, case)

        def boost(item_id: str) -> float:
            return WEIGHT * ((stats.used[item_id] + 1) / (stats.loaded[item_id] + 2) - 0.5)

        chosen = production_cut(items, key=lambda i: i.score + boost(i.id))
        if index >= len(cases) // 2:
            facts = [i for i in items if i.kind == "fact" and i.score >= 0.30]
            pools.append(
                {
                    "facts": [{"id": f.id, "score": f.score, "boost": boost(f.id)} for f in facts],
                    "replay_pick": [i.id for i in chosen if i.kind == "fact"],
                }
            )
        stats.learn(chosen, labels[case["run_id"]], when)
    (DATA_DIR / "verify_pools.json").write_text(json.dumps(pools))
    print(f"saved {len(pools)} pools")


if __name__ == "__main__":
    asyncio.run(main())
