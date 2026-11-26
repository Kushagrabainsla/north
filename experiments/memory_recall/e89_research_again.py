"""Issue #89: should a task search memory again when its focus changes, and on what trigger?

For each replayed task with two or more tool calls, each step may give a cue:
- path:   a file path that the prompt and earlier steps did not name
- site:   a web host that the prompt and earlier steps did not name
- name:   a capitalised name (two or more words) that is new to the task
- plan:   a step written with update_plan
- step:   any step at all (its tool input is the query), the "always" control

On a cue, recall runs with the cue text. An item is "added" when the start recall
did not hold it and it passes a filter:
- above_best:  its score is above the best score of the start recall (#89 as written)
- abs_0.6:     its score is 0.6 or more
- above_cut:   its score passes the start recall's own relative cutoff
At most 3 notes a task, never the same item twice (#89 step 3).

A judge marks each added item "needed" for the rest of the task. The control gives
each task the cues of an unrelated task: a good trigger adds almost nothing there.

Run: .venv/bin/python -m experiments.memory_recall.e89_research_again
"""

from __future__ import annotations

import asyncio
import random
import re
import statistics
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
    write_result,
)

LABEL_MODEL = "claude-haiku-5-5"
MAX_NOTES = 3
PER_NOTE = 5

PATH = re.compile(r"(?:~|\.{0,2})/[\w.\-/]+|\b[\w\-]+\.(?:py|md|ts|tsx|js|json|yaml|yml|toml|txt|csv|pdf|html|sh)\b")
HOST = re.compile(r"https?://([\w.\-]+)")
NAME = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")

SYSTEM = """You audit an AI assistant's memory. Partway through a task, extra memory items could be shown
to the assistant. You see the task, the actions after that point and the final answer.
Mark an item NEEDED when the later actions or the answer use the information it holds, or would clearly
be more correct or more fitting for this user with it. Same topic alone is NOT needed.
Judge every item id that you are given."""

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


def cues(case: dict[str, Any]) -> list[tuple[int, str, str]]:
    """(step index, cue kind, cue text) for every step of the task."""
    seen_text = case["prompt"]
    out = []
    for index, call in enumerate(case["tool_calls"]):
        params = call["params"]
        out.append((index, "step", f"{call['tool']} {params}"[:400]))
        for path in PATH.findall(params):
            if len(path) > 3 and path not in seen_text:
                out.append((index, "path", path))
        for host in HOST.findall(params):
            if host not in seen_text:
                out.append((index, "site", host))
        for name in NAME.findall(params):
            if name not in seen_text:
                out.append((index, "name", name))
        if call["tool"] == "update_plan":
            out.append((index, "plan", params))
        seen_text += "\n" + params
    return out


async def added_items(
    memory: MemorySnapshot, case: dict[str, Any], cue_list: list[tuple[int, str, str]], kind: str, rule: str
) -> list[tuple[int, Item]]:
    start = {i["id"] for i in case["facts"] + case["episodes"]}
    start_scores = [i["score"] for i in case["facts"]]
    best_start = max(start_scores) if start_scores else 0.0
    start_cut = max(0.30, best_start - 0.05)
    notes, added, seen = 0, [], set(start)
    for index, cue_kind, text in cue_list:
        if cue_kind != kind or notes >= MAX_NOTES:
            continue
        qvec = encode([text])[0]
        pool = (await memory.scored_facts(qvec, as_of=case["started_at"]))[:15] + memory.scored_episodes(
            qvec, as_of=case["started_at"]
        )[:3]
        if rule == "above_best":
            keep = [i for i in pool if i.score > best_start]
        elif rule == "abs_0.6":
            keep = [i for i in pool if i.score >= 0.6]
        else:
            keep = [i for i in pool if i.score >= start_cut]
        keep = [i for i in keep if i.id not in seen][:PER_NOTE]
        if keep:
            notes += 1
            seen |= {i.id for i in keep}
            added += [(index, i) for i in keep]
    return added


async def judge(case: dict[str, Any], added: list[tuple[int, Item]]) -> dict[str, bool]:
    if not added:
        return {}
    first = min(index for index, _ in added)
    later = case["tool_calls"][first:]
    lines = ["## Extra memory items"] + [f"[{i.id}] {i.text}" for _, i in added]
    lines.append("\n## Task\n" + case["prompt"][:3000])
    lines.append("\n## Actions from that point\n" + "\n".join(f"- {c['tool']}: {c['params']}" for c in later))
    lines.append("\n## Final answer\n" + case["output"][:6000])
    result = await ask_json(LABEL_MODEL, SYSTEM, "\n".join(lines), SCHEMA, effort="medium", max_tokens=6000)
    got = {r["id"]: bool(r["needed"]) for r in result["data"].get("items", [])}
    return {i.id: got.get(i.id, False) for _, i in added}


async def main() -> None:
    memory = MemorySnapshot()
    cases = [c for c in read_jsonl(DATA_DIR / "cases_asof.jsonl") if len(c["tool_calls"]) >= 2]
    rng = random.Random(89)
    unrelated = cases[:]
    rng.shuffle(unrelated)
    before = SPEND.usd
    report: dict[str, Any] = {"tasks": len(cases), "triggers": {}}
    for kind in ("path", "site", "name", "plan", "step"):
        for rule in ("above_best", "abs_0.6", "above_cut"):
            real, control = [], []
            for case, other in zip(cases, unrelated, strict=True):
                real.append(await added_items(memory, case, cues(case), kind, rule))
                control.append(await added_items(memory, case, cues(other), kind, rule) if other is not case else [])
            real_labels = await asyncio.gather(*(judge(c, a) for c, a in zip(cases, real, strict=True)))
            control_labels = await asyncio.gather(*(judge(c, a) for c, a in zip(cases, control, strict=True)))

            def summary(added: list[list[tuple[int, Item]]], labels: list[dict[str, bool]]) -> dict[str, Any]:
                fired = [a for a in added if a]
                items = sum(len(a) for a in added)
                needed = sum(sum(lab.values()) for lab in labels)
                return {
                    "fire_rate": round(len(fired) / len(added), 3),
                    "items_added_per_task": round(items / len(added), 2),
                    "tokens_added_per_task": round(statistics.mean(sum(tokens(i.text) for _, i in a) for a in added)),
                    "needed_share": round(needed / items, 3) if items else None,
                    "tasks_gaining_a_needed_item": sum(any(lab.values()) for lab in labels),
                }

            report["triggers"][f"{kind}/{rule}"] = {
                "real": summary(real, real_labels),
                "unrelated_control": summary(control, control_labels),
            }
            print(kind, rule, report["triggers"][f"{kind}/{rule}"])
    report["spend_usd_this_run"] = round(SPEND.usd - before, 2)
    path = write_result("e89_research_again", report)
    print(path.read_text())


if __name__ == "__main__":
    asyncio.run(main())
