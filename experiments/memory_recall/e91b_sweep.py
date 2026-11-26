"""Issue #91, round 2: weight sweep with confidence intervals, on the round 1 labels (no model calls).

Round 1 gave small gains (recall 0.698 -> 0.743 for the use score, 0.745 for
recency) on 42 evaluated tasks that need any item. This round asks whether the
gains are real: each setting is compared with similarity by a paired bootstrap
over tasks (2,000 resamples), and the 95% interval of the recall difference is
reported. A setting "wins" only when the whole interval is above zero.

Run: .venv/bin/python -m experiments.memory_recall.e91b_sweep
"""

from __future__ import annotations

import asyncio
import random
import statistics
from datetime import datetime
from typing import Any

from experiments.memory_recall.common import DATA_DIR, Item, MemorySnapshot, read_jsonl, tokens, write_result
from experiments.memory_recall.e91_fading import Stats, _when, candidates, production_cut

RESAMPLES = 2000


def make_policy(w_rate: float, w_fade: float, half_life: float, w_recency: float):
    def pick(items: list[Item], stats: Stats, when: datetime) -> list[Item]:
        def score(i: Item) -> float:
            value = i.score
            if w_rate:
                value += w_rate * ((stats.used[i.id] + 1) / (stats.loaded[i.id] + 2) - 0.5)
            if w_fade:
                last = stats.last_used.get(i.id)
                value += w_fade * ((0.5 ** ((when - last).days / half_life) if last else 0.5) - 0.5)
            if w_recency:
                stated = _when(i.meta.get("observed_at") or i.meta.get("timestamp"))
                age = (when - stated).days if stated else 365
                value += w_recency * (0.5 ** (max(age, 0) / half_life) - 0.5)
            return value

        return production_cut(items, key=score)

    return pick


def per_task(
    cases: list[dict[str, Any]], pools: dict[str, list[Item]], labels: dict[str, dict[str, bool]], pick
) -> list[dict[str, float]]:
    stats = Stats()
    half = len(cases) // 2
    out = []
    for index, case in enumerate(cases):
        when = _when(case["started_at"]) or datetime.now()
        needed = labels[case["run_id"]]
        chosen = pick(pools[case["run_id"]], stats, when)
        stats.learn(chosen, needed, when)
        if index < half:
            continue
        hits = sum(needed[i.id] for i in chosen)
        total = sum(needed.values())
        out.append(
            {
                "recall": hits / total if total else None,
                "precision": hits / len(chosen) if chosen else 0.0,
                "tokens": sum(tokens(i.text) for i in chosen),
            }
        )
    return out


def bootstrap_diff(a: list[float], b: list[float]) -> tuple[float, float, float]:
    rng = random.Random(91)
    n = len(a)
    diffs = []
    for _ in range(RESAMPLES):
        idx = [rng.randrange(n) for _ in range(n)]
        diffs.append(statistics.mean(a[i] - b[i] for i in idx))
    diffs.sort()
    return (
        statistics.mean(x - y for x, y in zip(a, b, strict=True)),
        diffs[int(0.025 * RESAMPLES)],
        diffs[int(0.975 * RESAMPLES)],
    )


async def main() -> None:
    memory = MemorySnapshot()
    cases = sorted(read_jsonl(DATA_DIR / "cases_asof.jsonl"), key=lambda c: c["started_at"])
    pools = {c["run_id"]: await candidates(memory, c) for c in cases}
    labels = {r["run_id"]: r["needed"] for r in read_jsonl(DATA_DIR / "e91_labels.jsonl")}
    base = per_task(cases, pools, labels, make_policy(0, 0, 60, 0))
    base_recall = [r["recall"] for r in base if r["recall"] is not None]
    report: dict[str, Any] = {
        "evaluated_tasks": len(base),
        "tasks_with_needed_items": len(base_recall),
        "similarity": {
            "recall": round(statistics.mean(base_recall), 3),
            "precision": round(statistics.mean(r["precision"] for r in base), 3),
            "tokens": round(statistics.mean(r["tokens"] for r in base)),
        },
        "settings": {},
    }
    grid = []
    for w_rate in (0.0, 0.03, 0.06, 0.1):
        for w_fade in (0.0, 0.03, 0.06):
            for w_recency in (0.0, 0.02, 0.04, 0.08):
                for half_life in (30.0, 60.0, 120.0):
                    if not (w_rate or w_fade or w_recency):
                        continue
                    if not (w_fade or w_recency) and half_life != 60.0:
                        continue
                    grid.append((w_rate, w_fade, half_life, w_recency))
    for w_rate, w_fade, half_life, w_recency in grid:
        rows = per_task(cases, pools, labels, make_policy(w_rate, w_fade, half_life, w_recency))
        recall = [r["recall"] for r in rows if r["recall"] is not None]
        mean, low, high = bootstrap_diff(recall, base_recall)
        name = f"rate={w_rate},fade={w_fade},recency={w_recency},half_life={half_life:g}"
        report["settings"][name] = {
            "recall": round(statistics.mean(recall), 3),
            "precision": round(statistics.mean(r["precision"] for r in rows), 3),
            "tokens": round(statistics.mean(r["tokens"] for r in rows)),
            "recall_gain": round(mean, 3),
            "ci95": [round(low, 3), round(high, 3)],
            "wins": low > 0,
        }
        report["settings"][name]["_recall_list"] = recall
    ranked = sorted(report["settings"].items(), key=lambda kv: kv[1]["recall_gain"], reverse=True)
    report["top5"] = [name for name, _ in ranked[:5]]
    report["settings_that_win"] = len([name for name, value in ranked if value["wins"]])

    # Held out: choose on the first half of the evaluated tasks, score on the second half.
    mid = len(base_recall) // 2

    def gain_on(name: str, part: slice) -> tuple[float, float, float]:
        return bootstrap_diff(report["settings"][name]["_recall_list"][part], base_recall[part])

    def parts(name: str) -> dict[str, float]:
        return {k: float(v) for k, v in (p.split("=") for p in name.split(","))}

    families = {
        "use_rate_only": lambda p: p["rate"] > 0 and p["fade"] == 0 and p["recency"] == 0,
        "use_rate_and_fade (issue #91)": lambda p: p["rate"] > 0 and p["fade"] > 0 and p["recency"] == 0,
        "recency_only (no #85 data)": lambda p: p["rate"] == 0 and p["fade"] == 0 and p["recency"] > 0,
        "use_rate_and_recency": lambda p: p["rate"] > 0 and p["fade"] == 0 and p["recency"] > 0,
        "any": lambda p: True,
    }
    held_out = {}
    for family, member in families.items():
        names = [n for n in report["settings"] if member(parts(n))]
        if not names:
            continue
        chosen = max(names, key=lambda n: gain_on(n, slice(0, mid))[0])
        mean, low, high = gain_on(chosen, slice(mid, None))
        held_out[family] = {
            "chosen_on_first_half": chosen,
            "gain_on_second_half": round(mean, 3),
            "ci95": [round(low, 3), round(high, 3)],
            "wins": low > 0,
        }
    report["held_out"] = held_out
    for value in report["settings"].values():
        value.pop("_recall_list")
    write_result("e91b_sweep", report)
    print({k: v for k, v in report.items() if k not in ("settings",)})
    for name in report["top5"]:
        print(name, report["settings"][name])


if __name__ == "__main__":
    asyncio.run(main())
