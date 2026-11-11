"""Turn a run's records into one table per question.   python report.py <results folder>"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

ARMS = ("north", "claude", "codex")


def load(folder: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(folder.glob("*__*__s*.json"))]


def verdict(record: dict) -> str:
    """One word for a run. An infrastructure error is not a wrong answer, and is kept apart."""
    score = record.get("score")
    if record.get("error") and not record.get("patch_bytes"):
        return "error"
    if score is None:
        return "unscored"
    if record.get("only_on_branch") and score.get("passed"):
        return "branch only"
    return "pass" if score.get("passed") else "fail"


def paired_wins(records: list[dict], first: str, second: str) -> tuple[int, int, int]:
    """On tasks both setups finished: how often the first passed where the second failed, and the reverse."""
    by: dict[tuple[int, int], dict[str, str]] = defaultdict(dict)
    for record in records:
        by[(record["number"], record["sample"])][record["arm"]] = verdict(record)
    wins = losses = ties = 0
    for cell in by.values():
        a, b = cell.get(first), cell.get(second)
        if a not in ("pass", "fail") or b not in ("pass", "fail"):
            continue
        if a == b:
            ties += 1
        elif a == "pass":
            wins += 1
        else:
            losses += 1
    return wins, losses, ties


def _fmt(values: list[float], pattern: str) -> str:
    return pattern.format(median(values)) if values else "n/a"


def render(records: list[dict]) -> str:
    numbers = sorted({r["number"] for r in records})
    cells: dict[tuple[int, str], list[str]] = defaultdict(list)
    for record in records:
        cells[(record["number"], record["arm"])].append(verdict(record))
    out = [
        "# North vs Claude vs Codex\n",
        "| # | level | benchmark | " + " | ".join(ARMS) + " |",
        "|---|---|---|---|---|---|",
    ]
    for number in numbers:
        sample = next(r for r in records if r["number"] == number)
        row = [", ".join(cells.get((number, arm), ["-"])) for arm in ARMS]
        out.append(f"| {number} | {sample['level']} | {sample['source']} | " + " | ".join(row) + " |")
    out += [
        "\n| setup | passed | failed | errors | unscored | median s | median tokens in |",
        "|---|---|---|---|---|---|---|",
    ]
    for arm in ARMS:
        mine = [r for r in records if r["arm"] == arm]
        verdicts = [verdict(r) for r in mine]
        seconds = [r["wall_seconds"] for r in mine]
        tokens = [r["tokens_in"] for r in mine if r.get("tokens_in")]
        out.append(
            f"| {arm} | {verdicts.count('pass')} | {verdicts.count('fail')} | {verdicts.count('error')} | "
            f"{verdicts.count('unscored')} | {_fmt(seconds, '{:.0f}')} | {_fmt(tokens, '{:,.0f}')} |"
        )
    out.append("\nNorth against each, on runs both finished:\n")
    for other in ("claude", "codex"):
        wins, losses, ties = paired_wins(records, "north", other)
        out.append(f"- vs {other}: north won {wins}, lost {losses}, tied {ties}")
    used = Counter(
        "+".join((r.get("extra") or {}).get("coding_agents") or ["none"]) for r in records if r["arm"] == "north"
    )
    if used:
        out.append("\nCoding agents north called, per run: " + ", ".join(f"{k} x{v}" for k, v in used.most_common()))
    failed = [r for r in records if r.get("setup_failed")]
    if failed:
        out.append(f"\nDependencies did not fully install for {len(failed)} runs, the same for every setup.")
    return "\n".join(out)


if __name__ == "__main__":
    print(render(load(Path(sys.argv[1]))))
