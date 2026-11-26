"""Issue #85: which method tells, cheaply and correctly, which loaded memory a task used?

Every replayed task gets its recalled items (facts, past-task notes, profile
sections). Each method marks each item "used" or "not used":

- reference: Opus 5.5 at medium effort (the labels the others are scored against)
- opus_again: the same reference call a second time, to see how stable the labels are
- sonnet: Sonnet 5.5, low effort
- haiku: Haiku 5.5, low effort (the method the issue proposes)
- embed: no model call; an item is used when its embedding is close to a part of the answer
- words: no model call; an item is used when enough of its rare words appear in the answer

Results: precision, recall, F1 and Cohen's kappa against the reference, cost and
time per task, and the used share per item kind (the "before" numbers for #85,
#86, #89 and #91).

Run: .venv/bin/python -m experiments.memory_recall.e85_attribution
"""

from __future__ import annotations

import asyncio
import random
import re
import statistics
import sys
from typing import Any

from experiments.memory_recall.common import (
    DATA_DIR,
    SPEND,
    Item,
    ask_json,
    cosine,
    encode,
    read_jsonl,
    write_jsonl,
    write_result,
)

OUTPUT_CHARS = 6000
STABILITY_CASES = 30

SYSTEM = """You audit an AI assistant's memory. Before a task, the assistant was given memory items:
facts about the user, notes from past tasks, and sections of the user's profile.
You see the task, the actions the assistant took and its final answer.

For each item, decide whether the assistant USED it: the answer or an action contains or depends on
information that the item gave. Examples of use: a name, a date, a preference, a goal, a rule or a
past outcome from the item shapes the answer or a tool input. An item is NOT used when the task
would have gone the same way without it, even if it is on the same topic.
Judge every item id that you are given. Be strict and consistent."""

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "used": {"type": "boolean"}},
                "required": ["id", "used"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

_STOP_WORDS = (
    "the a an and or of to in on for with at by from is are was were be been this that it its as not no "
    "user users north their they them has have had will would should can could may about into than then "
    "also only more most such over after before when what which who whom whose where why how"
)
STOP = frozenset(_STOP_WORDS.split())


def items_of(case: dict[str, Any]) -> list[Item]:
    return [Item(**i) for i in case["facts"] + case["episodes"] + case["profile"]]


def render(case: dict[str, Any], nonce: str = "") -> str:
    lines = ["## Memory items"]
    for item in items_of(case):
        text = item.text if item.kind != "profile" else item.text[:1500]
        lines.append(f"[{item.id}] {text}")
    lines.append("\n## Task\n" + case["prompt"][:3000])
    if case["tool_calls"]:
        lines.append("\n## Actions")
        lines.extend(f"- {c['tool']}: {c['params']}" for c in case["tool_calls"])
    lines.append("\n## Final answer\n" + case["output"][:OUTPUT_CHARS])
    if nonce:
        lines.append(f"\n(review pass {nonce})")
    return "\n".join(lines)


async def judge(model: str, case: dict[str, Any], effort: str, nonce: str = "") -> tuple[dict[str, bool], float]:
    result = await ask_json(model, SYSTEM, render(case, nonce), SCHEMA, effort=effort, max_tokens=8000)
    ids = {i.id for i in items_of(case)}
    labels = {row["id"]: bool(row["used"]) for row in result["data"].get("items", []) if row["id"] in ids}
    # An item the judge skipped counts as not used.
    return {i: labels.get(i, False) for i in ids}, result["seconds"]


def answer_text(case: dict[str, Any]) -> str:
    return case["output"][:OUTPUT_CHARS] + "\n" + "\n".join(c["params"] for c in case["tool_calls"])


def chunks(text: str, size: int = 400) -> list[str]:
    parts = [p.strip() for p in re.split(r"\n{1,}|(?<=[.!?])\s+", text) if p.strip()]
    out, current = [], ""
    for part in parts:
        if len(current) + len(part) > size and current:
            out.append(current)
            current = ""
        current = f"{current} {part}".strip()
    if current:
        out.append(current)
    return out or [text[:size]]


def embed_scores(case: dict[str, Any]) -> dict[str, float]:
    pieces = chunks(answer_text(case))
    piece_vecs = encode(pieces)
    out = {}
    for item in items_of(case):
        item_vec = encode([item.text[:1500]])[0]
        out[item.id] = max(cosine(item_vec, v) for v in piece_vecs)
    return out


def rare_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9'\-]{3,}", text.lower()) if w not in STOP}


def word_scores(case: dict[str, Any]) -> dict[str, float]:
    answer = rare_words(answer_text(case))
    out = {}
    for item in items_of(case):
        words = rare_words(item.text)
        out[item.id] = len(words & answer) / len(words) if words else 0.0
    return out


def agreement(pred: list[bool], ref: list[bool]) -> dict[str, float]:
    tp = sum(p and r for p, r in zip(pred, ref, strict=True))
    fp = sum(p and not r for p, r in zip(pred, ref, strict=True))
    fn = sum(r and not p for p, r in zip(pred, ref, strict=True))
    n = len(ref)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    observed = sum(p == r for p, r in zip(pred, ref, strict=True)) / n
    p_yes = (sum(pred) / n) * (sum(ref) / n)
    p_no = (1 - sum(pred) / n) * (1 - sum(ref) / n)
    expected = p_yes + p_no
    kappa = (observed - expected) / (1 - expected) if expected < 1 else 1.0
    return {
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "kappa": round(kappa, 3),
        "agreement": round(observed, 3),
    }


def best_threshold(scores: list[float], ref: list[bool]) -> float:
    candidates = sorted({round(s, 3) for s in scores})
    return max(candidates, key=lambda t: agreement([s >= t for s in scores], ref)["f1"]) if candidates else 1.0


async def main() -> None:
    cases = read_jsonl(DATA_DIR / "cases.jsonl")
    random.Random(85).shuffle(cases)
    if len(sys.argv) > 1:
        cases = cases[: int(sys.argv[1])]

    async def label(case: dict[str, Any]) -> dict[str, Any]:
        ref, ref_s = await judge("claude-opus-5-5", case, "medium")
        sonnet, sonnet_s = await judge("claude-sonnet-5-5", case, "low")
        haiku, haiku_s = await judge("claude-haiku-5-5", case, "low")
        row = {"run_id": case["run_id"], "reference": ref, "sonnet": sonnet, "haiku": haiku}
        row["seconds"] = {"reference": ref_s, "sonnet": sonnet_s, "haiku": haiku_s}
        return row

    before = SPEND.usd
    labelled = await asyncio.gather(*(label(c) for c in cases))
    stability = await asyncio.gather(
        *(judge("claude-opus-5-5", c, "medium", nonce="2") for c in cases[:STABILITY_CASES])
    )
    for row, (again, _) in zip(labelled, stability, strict=False):
        row["opus_again"] = again
    for case, row in zip(cases, labelled, strict=True):
        row["embed"] = embed_scores(case)
        row["words"] = word_scores(case)
        row["kinds"] = {i.id: i.kind for i in items_of(case)}
    write_jsonl(DATA_DIR / "e85_labels.jsonl", labelled)

    # Split in half: thresholds for the no-model methods are tuned on one half, scored on the other.
    half = len(labelled) // 2
    tune, test = labelled[:half], labelled[half:]

    def flat(rows: list[dict[str, Any]], method: str) -> tuple[list[Any], list[bool]]:
        pred, ref = [], []
        for row in rows:
            for item_id, truth in row["reference"].items():
                pred.append(row[method][item_id])
                ref.append(truth)
        return pred, ref

    report: dict[str, Any] = {"cases": len(labelled), "items": sum(len(r["reference"]) for r in labelled)}
    methods: dict[str, Any] = {}
    for method in ("sonnet", "haiku"):
        pred, ref = flat(test, method)
        methods[method] = agreement(pred, ref)
    for method in ("embed", "words"):
        scores, ref = flat(tune, method)
        threshold = best_threshold(scores, ref)
        pred, ref = flat(test, method)
        methods[method] = {**agreement([s >= threshold for s in pred], ref), "threshold": threshold}
    stable = [r for r in labelled if "opus_again" in r]
    pred, ref = flat(stable, "opus_again")
    methods["opus_again"] = {**agreement(pred, ref), "cases": len(stable)}
    for method in ("reference", "sonnet", "haiku"):
        secs = [r["seconds"][method] for r in labelled]
        methods.setdefault(method, {})["median_seconds"] = round(statistics.median(secs), 2)
    report["methods_on_test_half"] = methods

    # Used share per kind, from the reference labels (all cases).
    share: dict[str, Any] = {}
    for kind in ("fact", "episode", "profile"):
        loaded = used = 0
        for row in labelled:
            for item_id, truth in row["reference"].items():
                if row["kinds"][item_id] == kind:
                    loaded += 1
                    used += truth
        share[kind] = {"loaded": loaded, "used": used, "used_share": round(used / loaded, 3) if loaded else 0.0}
    report["used_share_by_kind"] = share
    tasks_using_any = sum(any(r["reference"].values()) for r in labelled)
    report["tasks_using_any_memory"] = round(tasks_using_any / len(labelled), 3)
    report["spend_usd_this_run"] = round(SPEND.usd - before, 2)
    path = write_result("e85_attribution", report)
    print(path.read_text())


if __name__ == "__main__":
    asyncio.run(main())
