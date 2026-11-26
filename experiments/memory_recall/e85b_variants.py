"""Issue #85, round 2: can a cheap judge reach the reference?

Round 1 (e85_attribution): Haiku 5.5 at low effort agreed with the Opus reference
at kappa 0.58, Sonnet 5.5 at 0.76. This round tries, on the same test half:

- haiku_medium:   Haiku 5.5, medium effort, same prompt
- haiku_quote:    Haiku 5.5, low effort, must quote the words of the answer or action
                  that use the item; "used" counts only when the quote is found there
- sonnet_quote:   Sonnet 5.5, low effort, the same quote rule
- haiku_quote_medium

The quote check is plain text matching, so it costs nothing and cannot be talked round.

Run: .venv/bin/python -m experiments.memory_recall.e85b_variants
"""

from __future__ import annotations

import asyncio
import random
import re
import statistics
from typing import Any

from experiments.memory_recall.common import DATA_DIR, SPEND, ask_json, read_jsonl, write_result
from experiments.memory_recall.e85_attribution import SYSTEM, agreement, answer_text, items_of, judge, render

QUOTE_SYSTEM = (
    SYSTEM
    + """

For each item you mark used, copy into "quote" the exact words (5 to 25 words) of the final answer or of
an action that show the use. Copy them character for character. For an unused item, leave "quote" empty."""
)

QUOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "used": {"type": "boolean"}, "quote": {"type": "string"}},
                "required": ["id", "used", "quote"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()


def quote_found(quote: str, haystack: str) -> bool:
    """True when most of the quote's words appear, in order, in the answer or actions."""
    words = _norm(quote).split()
    if len(words) < 3:
        return False
    target = _norm(haystack)
    if " ".join(words) in target:
        return True
    # Allow small copy slips: 80% of 4-word windows must be found.
    windows = [" ".join(words[i : i + 4]) for i in range(max(1, len(words) - 3))]
    return sum(w in target for w in windows) / len(windows) >= 0.8


async def judge_quote(model: str, case: dict[str, Any], effort: str) -> tuple[dict[str, bool], dict[str, bool], float]:
    result = await ask_json(model, QUOTE_SYSTEM, render(case), QUOTE_SCHEMA, effort=effort, max_tokens=8000)
    ids = {i.id for i in items_of(case)}
    haystack = answer_text(case)
    claimed, checked = {}, {}
    for row in result["data"].get("items", []):
        if row["id"] in ids:
            claimed[row["id"]] = bool(row["used"])
            checked[row["id"]] = bool(row["used"]) and quote_found(row.get("quote", ""), haystack)
    return {i: claimed.get(i, False) for i in ids}, {i: checked.get(i, False) for i in ids}, result["seconds"]


async def main() -> None:
    cases = read_jsonl(DATA_DIR / "cases.jsonl")
    random.Random(85).shuffle(cases)
    labelled = {r["run_id"]: r for r in read_jsonl(DATA_DIR / "e85_labels.jsonl")}
    cases = [c for c in cases if c["run_id"] in labelled]
    test = cases[len(cases) // 2 :]
    before = SPEND.usd

    async def one(case: dict[str, Any]) -> dict[str, Any]:
        row: dict[str, Any] = {"reference": labelled[case["run_id"]]["reference"], "seconds": {}}
        row["haiku_medium"], row["seconds"]["haiku_medium"] = await judge("claude-haiku-5-5", case, "medium")
        for name, model, effort in (
            ("haiku_quote", "claude-haiku-5-5", "low"),
            ("haiku_quote_medium", "claude-haiku-5-5", "medium"),
            ("sonnet_quote", "claude-sonnet-5-5", "low"),
        ):
            claimed, checked, seconds = await judge_quote(model, case, effort)
            row[f"{name}_claimed"], row[name], row["seconds"][name] = claimed, checked, seconds
        return row

    rows = await asyncio.gather(*(one(c) for c in test))
    report: dict[str, Any] = {"test_cases": len(rows), "methods": {}}
    names = ["haiku_medium", "haiku_quote_claimed", "haiku_quote", "haiku_quote_medium_claimed", "haiku_quote_medium"]
    names += ["sonnet_quote_claimed", "sonnet_quote"]
    for name in names:
        pred, ref = [], []
        for row in rows:
            for item_id, truth in row["reference"].items():
                pred.append(row[name][item_id])
                ref.append(truth)
        entry = agreement(pred, ref)
        base = name.removesuffix("_claimed")
        entry["median_seconds"] = round(statistics.median(r["seconds"][base] for r in rows), 2)
        report["methods"][name] = entry
    report["spend_usd_this_run"] = round(SPEND.usd - before, 2)
    path = write_result("e85b_variants", report)
    print(path.read_text())


if __name__ == "__main__":
    asyncio.run(main())
