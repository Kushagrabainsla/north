"""Issue #86, round 2: keep the profile, but fix how it is given.

Round 1 (e86_profile): removing most of the profile lowered answer grades
(1.32 -> 1.24, and 1.4 -> 1.0 on broad questions), and a read_profile tool was
called on 9% of questions, so it did not help (1.06). This round tries:

- full:        production today (cut at 12,000 characters, which cuts the judgement rules)
- full_again:  the same, answered and graded again, to measure the noise
- whole:       the whole profile, no cut
- whole_dedup: the whole profile, and no recalled fact that the profile already holds
- lean:        whole_dedup without the four sections tasks used least
               (Skills, Health, Finances, Other: 6 uses in 120 tasks together)

Run: .venv/bin/python -m experiments.memory_recall.e86b_variants
"""

from __future__ import annotations

import asyncio
import random
import statistics
from typing import Any

from experiments.memory_recall.common import (
    DATA_DIR,
    SPEND,
    MemorySnapshot,
    ask_with_tools,
    profile_sections,
    profile_text,
    read_jsonl,
    tokens,
    write_result,
)
from experiments.memory_recall.e86_profile import ANSWER_MODEL, SYSTEM, grade, questions

RARE = {"profile:Skills", "profile:Health", "profile:Finances", "profile:Other"}
VARIANTS = ("full", "full_again", "whole", "whole_dedup", "lean")


def in_profile(fact_text: str, profile: str) -> bool:
    core = fact_text.split(" (")[0].strip().rstrip(".").lower()
    return bool(core) and core in profile


async def answer(variant: str, question: dict[str, Any], memory: MemorySnapshot) -> dict[str, Any]:
    sections = profile_sections()
    whole = "\n\n".join(s.text for s in sections)
    lowered = whole.lower()
    recalled = await memory.recall(question["question"])
    facts = [f.text for f in recalled["facts"]]
    if variant in ("full", "full_again"):
        body = whole[:12_000]
    elif variant == "whole":
        body = whole
    else:
        facts = [f for f in facts if not in_profile(f, lowered)]
        keep = [s for s in sections if variant != "lean" or s.id not in RARE]
        body = "\n\n".join(s.text for s in keep)
    block = body + ("\n\n## Personal Context\n" + "\n".join(f"- {f}" for f in facts) if facts else "")
    system = f"{SYSTEM}\n\n# Memory\n{block}"
    if variant == "full_again":
        system += "\n"  # a different request, so the cache does not answer it
    turn = await ask_with_tools(ANSWER_MODEL, system, [{"role": "user", "content": question["question"]}], [])
    text = "\n".join(b.get("text", "") for b in turn["content"] if b["type"] == "text")
    return {"text": text, "start_tokens": tokens(block)}


async def main() -> None:
    cases = read_jsonl(DATA_DIR / "cases.jsonl")
    labels = {r["run_id"]: r for r in read_jsonl(DATA_DIR / "e85_labels.jsonl")}
    memory = MemorySnapshot()
    qs = await questions(cases, labels, profile_sections())
    random.Random(86).shuffle(qs)
    full_profile = profile_text()
    before = SPEND.usd

    async def run(q: dict[str, Any], variant: str) -> dict[str, Any]:
        reply = await answer(variant, q, memory)
        graded = await grade(q, reply["text"] + ("\n" if variant == "full_again" else ""), full_profile)
        return {"source": q["source"].split(":")[0], "variant": variant, "q": q["question"][:80], **reply, **graded}

    rows = await asyncio.gather(*(run(q, v) for q in qs for v in VARIANTS))
    report: dict[str, Any] = {"questions": len(qs), "variants": {}}
    for variant in VARIANTS:
        sel = [r for r in rows if r["variant"] == variant]
        report["variants"][variant] = {
            "mean_score": round(statistics.mean(r["score"] for r in sel), 3),
            "full_marks": round(sum(r["score"] == 2 for r in sel) / len(sel), 3),
            "invented": sum(r["invented"] for r in sel),
            "mean_start_tokens": round(statistics.mean(r["start_tokens"] for r in sel)),
            "broad_mean_score": round(statistics.mean(r["score"] for r in sel if r["source"] == "broad"), 3),
        }
    # Paired noise: how often the same answer setup gets a different grade on a second run.
    by_q = {}
    for r in rows:
        by_q.setdefault(r["q"], {})[r["variant"]] = r["score"]
    report["noise_full_vs_full_again"] = {
        "same_grade_share": round(statistics.mean(v["full"] == v["full_again"] for v in by_q.values()), 3),
        "mean_abs_diff": round(statistics.mean(abs(v["full"] - v["full_again"]) for v in by_q.values()), 3),
    }
    for variant in ("whole", "whole_dedup", "lean"):
        diffs = [v[variant] - v["full"] for v in by_q.values()]
        report["variants"][variant]["paired_diff_vs_full"] = round(statistics.mean(diffs), 3)
    report["spend_usd_this_run"] = round(SPEND.usd - before, 2)
    path = write_result("e86b_variants", report)
    print(path.read_text())


if __name__ == "__main__":
    asyncio.run(main())
