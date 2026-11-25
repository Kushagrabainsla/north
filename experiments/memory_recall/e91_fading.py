"""Issue #91: does a use score (with fading) rank memory better than similarity alone?

For each replayed task, the 30 closest facts and 6 closest past-task notes are
candidates. A judge marks each candidate "needed" (the task's answer or actions use
it, or would clearly be better with it). The tasks then run in time order. Each
policy picks what to load; after each task the policy learns which of the loaded
items were needed, as the #85 log would tell it. Policies are scored on the later
half of the tasks.

Policies:
- similarity:  production (relative cutoff, at most 15 facts and 3 notes)
- issue_91:    similarity + w * use rate (used+1)/(loaded+2) + w * fading (half-life 60 days)
- memrl:       (1-l) * z(similarity) + l * z(Q), Q <- Q + a * (used - Q)  (MemRL, arXiv 2601.03192)
- recency:     similarity + w * how recently the fact was stated
- tighter:     similarity with a smaller cap (5 facts, 2 notes); no use data at all
Each use-based policy is also tried with one exploration place (#91 step 4).

Metrics: needed share of loaded items (precision), share of needed items loaded
(recall), items and tokens loaded per task.

Run: .venv/bin/python -m experiments.memory_recall.e91_fading
"""

from __future__ import annotations

import asyncio
import random
import statistics
from collections import defaultdict
from datetime import datetime
from typing import Any

from experiments.memory_recall.common import (
    DATA_DIR,
    SPEND,
    Item,
    MemorySnapshot,
    ask_json,
    encode,
    read_jsonl,
    tokens,
    write_jsonl,
    write_result,
)

FACT_CANDIDATES = 30
EPISODE_CANDIDATES = 6
LABEL_MODEL = "claude-haiku-5-5"

SYSTEM = """You audit an AI assistant's memory. You see a task, the actions the assistant took, its final
answer, and candidate memory items. The assistant may or may not have seen each item.

Mark an item NEEDED when the answer or an action uses the information it holds, or when the answer
would clearly be more correct or more fitting for this user with it. An item that is only on the same
topic, or that would not change anything, is NOT needed. Judge every item id that you are given."""

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "needed": {"type": "boolean"}},
                "required": ["id", "needed"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


async def candidates(memory: MemorySnapshot, case: dict[str, Any]) -> list[Item]:
    qvec = encode([case["prompt"]])[0]
    facts = (await memory.scored_facts(qvec, as_of=case["started_at"]))[:FACT_CANDIDATES]
    episodes = memory.scored_episodes(qvec, as_of=case["started_at"])[:EPISODE_CANDIDATES]
    return facts + episodes


async def label(case: dict[str, Any], items: list[Item]) -> dict[str, bool]:
    order = items[:]
    random.Random(case["run_id"]).shuffle(order)  # the judge must not see the rank
    lines = ["## Candidate memory items"] + [f"[{i.id}] {i.text}" for i in order]
    lines.append("\n## Task\n" + case["prompt"][:3000])
    if case["tool_calls"]:
        lines.append("\n## Actions\n" + "\n".join(f"- {c['tool']}: {c['params']}" for c in case["tool_calls"]))
    lines.append("\n## Final answer\n" + case["output"][:6000])
    result = await ask_json(LABEL_MODEL, SYSTEM, "\n".join(lines), SCHEMA, effort="medium", max_tokens=8000)
    got = {r["id"]: bool(r["needed"]) for r in result["data"].get("items", [])}
    return {i.id: got.get(i.id, False) for i in items}


# --------------------------------------------------------------------------- #
# Policies


def _when(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")[:19])
    except ValueError:
        return None


class Stats:
    def __init__(self) -> None:
        self.loaded: dict[str, int] = defaultdict(int)
        self.used: dict[str, int] = defaultdict(int)
        self.last_used: dict[str, datetime] = {}
        self.q: dict[str, float] = defaultdict(float)

    def learn(self, loaded: list[Item], needed: dict[str, bool], when: datetime, alpha: float = 0.3) -> None:
        for item in loaded:
            self.loaded[item.id] += 1
            used = needed.get(item.id, False)
            self.q[item.id] += alpha * ((1.0 if used else 0.0) - self.q[item.id])
            if used:
                self.used[item.id] += 1
                self.last_used[item.id] = when


def production_cut(items: list[Item], key: Any, fact_cap: int = 15, episode_cap: int = 3) -> list[Item]:
    """The production shape: facts with the relative cutoff on *key*, notes by rank."""
    facts = sorted((i for i in items if i.kind == "fact"), key=key, reverse=True)
    episodes = sorted((i for i in items if i.kind == "episode"), key=key, reverse=True)
    kept: list[Item] = []
    if facts:
        best = key(facts[0])
        for rank, f in enumerate(facts[:fact_cap]):
            if f.score >= 0.30 and (rank < 3 or key(f) >= best - 0.05):
                kept.append(f)
    kept += [e for e in episodes if e.score > 0.3][:episode_cap]
    return kept


def zscores(values: list[float]) -> list[float]:
    if len(values) < 2:
        return [0.0 for _ in values]
    mean, sd = statistics.mean(values), statistics.pstdev(values) or 1.0
    return [(v - mean) / sd for v in values]


def pick(policy: str, items: list[Item], stats: Stats, when: datetime, explore: bool, rng: random.Random) -> list[Item]:
    if policy == "similarity":
        return production_cut(items, key=lambda i: i.score)
    if policy == "tighter":
        return production_cut(items, key=lambda i: i.score, fact_cap=5, episode_cap=2)
    if policy == "issue_91":
        w_rate, w_fade, half_life = 0.06, 0.03, 60.0

        def score(i: Item) -> float:
            rate = (stats.used[i.id] + 1) / (stats.loaded[i.id] + 2)
            last = stats.last_used.get(i.id)
            fade = 0.5 ** ((when - last).days / half_life) if last else 0.5
            return i.score + w_rate * (rate - 0.5) + w_fade * (fade - 0.5)

        chosen = production_cut(items, key=score)
    elif policy == "memrl":
        lam = 0.3
        sims = zscores([i.score for i in items])
        qs = zscores([stats.q[i.id] if stats.loaded[i.id] else 0.5 for i in items])
        mixed = {i.id: (1 - lam) * s + lam * q for i, s, q in zip(items, sims, qs, strict=True)}
        # The z-scored mix changes the scale, so the cutoff is applied to similarity
        # and the mix only orders the items.
        chosen = production_cut(items, key=lambda i: mixed[i.id])
    elif policy == "recency":

        def score(i: Item) -> float:
            stated = _when(i.meta.get("observed_at") or i.meta.get("timestamp"))
            age = (when - stated).days if stated else 365
            return i.score + 0.03 * (0.5 ** (max(age, 0) / 60.0) - 0.5)

        chosen = production_cut(items, key=score)
    else:
        raise ValueError(policy)
    if explore:
        # One place for a faded item that still matches: the best-matching item that
        # was loaded at least twice and never used.
        faded = [
            i
            for i in items
            if i not in chosen
            and i.kind == "fact"
            and stats.loaded[i.id] >= 2
            and not stats.used[i.id]
            and i.score >= 0.5
        ]
        if faded:
            facts = [c for c in chosen if c.kind == "fact"]
            if len(facts) >= 15:
                chosen.remove(facts[-1])
            chosen.append(rng.choice(faded[:5]))
    return chosen


def evaluate(
    cases: list[dict[str, Any]],
    pools: dict[str, list[Item]],
    labels: dict[str, dict[str, bool]],
    policy: str,
    explore: bool,
) -> dict[str, Any]:
    stats = Stats()
    rng = random.Random(91)
    half = len(cases) // 2
    precision, recall, counts, token_counts = [], [], [], []
    for index, case in enumerate(cases):
        when = _when(case["started_at"]) or datetime.now()
        items = pools[case["run_id"]]
        needed = labels[case["run_id"]]
        chosen = pick(policy, items, stats, when, explore, rng)
        stats.learn(chosen, needed, when)
        if index < half:
            continue
        hits = sum(needed[i.id] for i in chosen)
        total_needed = sum(needed.values())
        counts.append(len(chosen))
        token_counts.append(sum(tokens(i.text) for i in chosen))
        if chosen:
            precision.append(hits / len(chosen))
        if total_needed:
            recall.append(hits / total_needed)
    return {
        "precision": round(statistics.mean(precision), 3),
        "recall": round(statistics.mean(recall), 3) if recall else None,
        "tasks_with_needed_items": len(recall),
        "items_per_task": round(statistics.mean(counts), 2),
        "tokens_per_task": round(statistics.mean(token_counts)),
    }


async def main() -> None:
    memory = MemorySnapshot()
    cases = sorted(read_jsonl(DATA_DIR / "cases_asof.jsonl"), key=lambda c: c["started_at"])
    pools = {c["run_id"]: await candidates(memory, c) for c in cases}
    before = SPEND.usd
    results = await asyncio.gather(*(label(c, pools[c["run_id"]]) for c in cases))
    labels = {c["run_id"]: r for c, r in zip(cases, results, strict=True)}
    write_jsonl(DATA_DIR / "e91_labels.jsonl", [{"run_id": k, "needed": v} for k, v in labels.items()])

    needed_ranks: list[int] = []
    for case in cases:
        for rank, item in enumerate(i for i in pools[case["run_id"]] if i.kind == "fact"):
            if labels[case["run_id"]][item.id]:
                needed_ranks.append(rank + 1)
    report: dict[str, Any] = {
        "tasks": len(cases),
        "evaluated_tasks": len(cases) - len(cases) // 2,
        "needed_facts_by_rank": {
            "1-5": sum(r <= 5 for r in needed_ranks),
            "6-15": sum(5 < r <= 15 for r in needed_ranks),
            "16-30": sum(r > 15 for r in needed_ranks),
        },
        "tasks_needing_any_candidate": sum(any(v.values()) for v in labels.values()),
        "policies": {},
    }
    for policy in ("similarity", "tighter", "issue_91", "memrl", "recency"):
        report["policies"][policy] = evaluate(cases, pools, labels, policy, explore=False)
        if policy in ("issue_91", "memrl"):
            report["policies"][f"{policy}+explore"] = evaluate(cases, pools, labels, policy, explore=True)
    report["spend_usd_this_run"] = round(SPEND.usd - before, 2)
    path = write_result("e91_fading", report)
    print(path.read_text())


if __name__ == "__main__":
    asyncio.run(main())
