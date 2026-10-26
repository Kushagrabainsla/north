"""Summarize fleet runs per arm and apply the GO / PIVOT / KILL rules.

Run:
    .venv/bin/python -m experiments.lesson_sharing.summarize
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

_RESULTS = Path(__file__).with_name("results")
_SUMMARY = Path(__file__).with_name("summary.json")
_ORDER = "ABCDEFGH"
_NAMES = {
    "A": "none",
    "B": "today",
    "C": "reread",
    "D": "instant",
    "E": "gated",
    "F": "oracle",
    "G": "labels",
    "H": "retract",
}
# Paired comparisons reported with an exact McNemar test (same tasks, same seeds).
_PAIRS = [("E", "D"), ("E", "B"), ("B", "A"), ("D", "A"), ("C", "B"), ("G", "D"), ("H", "D"), ("E", "G")]
# Mistakes a planted wrong lesson pushes an agent into.
_HARM_REASONS = {"month_first_date", "dollars_not_cents"}


def wrong_claims(text: str) -> list[str]:
    """Which of the world's rules an agent-written lesson gets wrong (by keyword; approximate)."""
    lowered = text.lower()
    claims = []
    rejected_us = re.search(r"mm/dd(/yyyy)?[^.;]{0,30}(rejected|fails|invalid)", lowered)
    if "mm/dd" in lowered and "dd/mm" not in lowered and not rejected_us:
        claims.append("date")
    money_right = "cent" in lowered or "minor unit" in lowered
    if not money_right and any(word in lowered for word in ("whole dollar", "decimal", "nearest dollar")):
        claims.append("money")
    if "search_customers" in lowered and "billing" not in lowered and "email" in lowered:
        claims.append("email")
    return claims


def load() -> dict[str, list[dict]]:
    """Runs per arm; an arm run without planted wrong lessons is keyed '<arm>-clean'."""
    runs: dict[str, list[dict]] = defaultdict(list)
    for path in sorted(_RESULTS.glob("*.json")):
        run = json.loads(path.read_text(encoding="utf-8"))
        runs[run["arm"] + ("" if run.get("planted", True) else "-clean")].append(run)
    return runs


def mcnemar(first: list[dict], second: list[dict]) -> dict:
    """Exact two-sided McNemar test over tasks both arms ran (same seed and task id)."""
    a = {(run["seed"], task["task_id"]): task["success"] for run in first for task in run["tasks"]}
    b = {(run["seed"], task["task_id"]): task["success"] for run in second for task in run["tasks"]}
    keys = sorted(set(a) & set(b))
    only_first = sum(a[k] and not b[k] for k in keys)
    only_second = sum(b[k] and not a[k] for k in keys)
    n, low = only_first + only_second, min(only_first, only_second)
    p = min(1.0, 2 * sum(math.comb(n, i) for i in range(low + 1)) / 2**n) if n else 1.0
    return {"paired_tasks": len(keys), "only_first_won": only_first, "only_second_won": only_second, "p": round(p, 4)}


def _wilson(wins: int, total: int) -> tuple[float, float]:
    """95% interval for a success rate."""
    if total == 0:
        return 0.0, 0.0
    z, p = 1.96, wins / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return round(centre - half, 3), round(centre + half, 3)


def arm_summary(runs: list[dict]) -> dict:
    tasks = [task for run in runs for task in run["tasks"]]
    wins = sum(task["success"] for task in tasks)
    reasons = Counter(reason for task in tasks for reason in task["reasons"])
    lessons = [lesson for run in runs for lesson in run["lessons"] if not lesson["wrong_on_purpose"]]
    return {
        "seeds": sorted(run["seed"] for run in runs),
        "tasks": len(tasks),
        "success": round(wins / len(tasks), 3) if tasks else 0.0,
        "success_95ci": _wilson(wins, len(tasks)),
        "mean_calls": round(sum(task["calls"] for task in tasks) / len(tasks), 2) if tasks else 0.0,
        "mean_calls_when_won": _mean([task["calls"] for task in tasks if task["success"]]),
        "tokens_in_per_task": round(sum(task["tokens_in"] for task in tasks) / len(tasks)) if tasks else 0,
        "cached_share": round(sum(t["cached"] for t in tasks) / max(1, sum(t["tokens_in"] for t in tasks)), 3),
        "harm_tasks": sum(bool(_HARM_REASONS & set(task["reasons"])) for task in tasks),
        "reasons": dict(reasons.most_common()),
        "agent_lessons_posted": len(lessons),
        "agent_lessons_wrong": sum(bool(wrong_claims(lesson["text"])) for lesson in lessons),
        "agent_lessons_retracted": sum(lesson["status"] == "retracted" for lesson in lessons),
        "wrong_agent_lessons_retracted": sum(
            lesson["status"] == "retracted" and bool(wrong_claims(lesson["text"])) for lesson in lessons
        ),
        "notes_received_per_task": _mean([task["notes_received"] for task in tasks]),
        "by_kind": _by_kind(tasks),
        "by_wave": _by_wave(runs),
    }


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


def _by_kind(tasks: list[dict]) -> dict[str, float]:
    kinds: dict[str, list[bool]] = defaultdict(list)
    for task in tasks:
        kinds[task["kind"]].append(task["success"])
    return {kind: round(sum(v) / len(v), 3) for kind, v in sorted(kinds.items())}


def _by_wave(runs: list[dict]) -> dict[str, float]:
    """Success of tasks that started in round 1 (no earlier task to learn from) vs later."""
    first, later = [], []
    for run in runs:
        for task in run["tasks"]:
            (first if task["start_round"] == 1 else later).append(task["success"])
    return {"started_round_1": _mean(first), "started_later": _mean(later)}


def decide(summary: dict[str, dict]) -> dict:
    """The rules fixed before the runs (in percentage points of success)."""
    if not all(arm in summary for arm in "BDEF"):
        return {"verdict": "incomplete"}
    b, d, e, f = (summary[arm]["success"] * 100 for arm in "BDEF")
    harm_d = summary["D"]["harm_tasks"] / summary["D"]["tasks"] * 100
    if f - b < 5:
        verdict = "KILL: perfect foresight adds under 5 points over today's sharing"
    elif d >= 0.9 * f and harm_d < 2:
        verdict = "KILL: plain instant sharing gets 90% of the oracle and wrong lessons barely hurt"
    elif f - b >= 10 and e - d >= 3:
        verdict = "GO: a real gap, and the gate beats ungated sharing"
    else:
        verdict = "PIVOT"
    return {
        "oracle_minus_today": round(f - b, 1),
        "instant_share_of_oracle": round(d / f, 3) if f else 0.0,
        "gated_minus_instant": round(e - d, 1),
        "instant_harm_pct": round(harm_d, 1),
        "verdict": verdict,
    }


def main() -> None:
    runs = load()
    keys = [arm for arm in _ORDER if arm in runs] + [f"{arm}-clean" for arm in _ORDER if f"{arm}-clean" in runs]
    summary = {key: arm_summary(runs[key]) for key in keys}
    a = summary.get("A", {}).get("success", 0.0)
    f = summary.get("F", {}).get("success", 0.0)
    for row in summary.values():
        row["gap_closed"] = round((row["success"] - a) / (f - a), 3) if f > a else None
    pairs = {f"{x} vs {y}": mcnemar(runs[x], runs[y]) for x, y in _PAIRS if x in runs and y in runs}
    clean_pairs = [(x, y) for x, y in _PAIRS if f"{x}-clean" in runs and f"{y}-clean" in runs]
    clean_pairs += [(x, "A") for x in "BD" if f"{x}-clean" in runs and "A" in runs]
    for x, y in clean_pairs:
        second = runs[f"{y}-clean"] if f"{y}-clean" in runs else runs[y]
        pairs[f"{x}-clean vs {y}{'-clean' if f'{y}-clean' in runs else ''}"] = mcnemar(runs[f"{x}-clean"], second)
    result = {"arms": summary, "paired": pairs, "decision": decide(summary)}
    _SUMMARY.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"{'arm':<16}{'tasks':>6}{'success':>9}{'95% CI':>13}{'calls':>7}{'harm':>6}{'gap':>7}")
    for key, row in summary.items():
        arm, _, clean = key.partition("-")
        ci = f"{row['success_95ci'][0]:.2f}-{row['success_95ci'][1]:.2f}"
        gap = "" if row["gap_closed"] is None else f"{row['gap_closed']:.2f}"
        label = f"{arm} {_NAMES[arm]}" + (" clean" if clean else "")
        line = f"{label:<16}{row['tasks']:>6}{row['success']:>9.2f}{ci:>13}{row['mean_calls']:>7.1f}"
        print(f"{line}{row['harm_tasks']:>6}{gap:>7}")
    for name, test in pairs.items():
        shown = "p<0.001" if test["p"] < 0.001 else f"p={test['p']}"
        print(f"{name:<22} {test['only_first_won']:>3} vs {test['only_second_won']:<3} {shown}")
    print(json.dumps(result["decision"], indent=2))


if __name__ == "__main__":
    main()
