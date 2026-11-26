"""Issue #86: must every task carry the whole profile?

Profile variants:
- full:      the whole profile, cut at 12,000 characters (production today)
- core:      only the core sections (identity, preferences, the facts the user stated, goals)
- core_sim:  core, plus the other sections whose embedding is close to the task
- core_tool: core, plus a read_profile tool that returns any other section
- both:      core_sim plus the read_profile tool

Part 1 (offline, all replayed tasks): memory tokens at task start, and how many of
the profile sections that the #85 reference marked "used" each variant still gives.

Part 2 (live): questions about the user, answered by Opus 5.5 with each
variant, and graded by Sonnet 5.5 against the full profile. Real past
prompts that used a non-core section, broad questions ("who am I"), one question
per non-core section, and tasks that are not about the user.

Run: .venv/bin/python -m experiments.memory_recall.e86_profile
"""

from __future__ import annotations

import asyncio
import json
import random
import statistics
import sys
from typing import Any

from experiments.memory_recall.common import (
    DATA_DIR,
    SPEND,
    Item,
    ask_json,
    ask_with_tools,
    cosine,
    encode,
    profile_sections,
    read_jsonl,
    tokens,
    write_jsonl,
    write_result,
)

# Always shown. "User" holds what the user stated in conversation; north stars are the goals.
# Judgement rules joined the core after the #85 labels: tasks used them in 58 of 120 runs.
CORE = (
    "profile:User Profile",
    "profile:Identity",
    "profile:Preferences",
    "profile:User",
    "profile:North stars (your goals)",
    "profile:Judgement rules",
)
ANSWER_MODEL = "claude-opus-5-5"
GRADER_MODEL = "claude-sonnet-5-5"
REAL_QUESTIONS = 20
OTHER_TASKS = 10

SYSTEM = """You are north, the user's personal assistant. Answer the user's request directly and briefly.
Use what you know about the user when it matters. Do not invent facts about the user.
If you cannot do an action (you have no tools for it here), say in one line what you would do, then answer."""

READ_PROFILE = {
    "name": "read_profile",
    "description": (
        "Read sections of the user's profile that are not shown above: jobs, education, projects, skills, "
        "schedule, health, finances, judgement rules, other. Call it when the request needs facts about "
        "the user that the shown memory does not hold. Leave sections empty to read the whole profile."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"sections": {"type": "array", "items": {"type": "string"}}},
        "additionalProperties": False,
    },
}

GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "enum": [0, 1, 2]},
        "invented": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["score", "invented", "reason"],
    "additionalProperties": False,
}

GRADER = """You grade an assistant's answer for a user whose full profile you can see.
Score 2: the answer uses the right facts about the user and is correct and complete for what was asked.
Score 1: partly right, or it misses user facts that the request needed.
Score 0: wrong, or it ignores user facts that the request clearly needed.
For a request that does not need user facts, judge only whether the answer is correct and useful.
Set invented=true when the answer states a fact about the user that the profile does not support."""

QUESTION_SCHEMA = {
    "type": "object",
    "properties": {"question": {"type": "string"}},
    "required": ["question"],
    "additionalProperties": False,
}


def section_vectors(sections: list[Item]) -> dict[str, list[float]]:
    vecs = encode([s.text[:1500] for s in sections])
    return {s.id: v for s, v in zip(sections, vecs, strict=True)}


def similar_sections(
    query_vec: list[float], sections: list[Item], vecs: dict[str, list[float]], margin: float, floor: float
) -> list[str]:
    scored = sorted(((cosine(query_vec, vecs[s.id]), s.id) for s in sections if s.id not in CORE), reverse=True)
    if not scored:
        return []
    best = scored[0][0]
    return [sid for score, sid in scored if score >= floor and score >= best - margin]


def render_memory(sections: list[Item], shown: set[str], facts: list[dict[str, Any]], full: bool) -> str:
    if full:
        body = "\n\n".join(s.text for s in sections)[:12_000]
    else:
        body = "\n\n".join(s.text for s in sections if s.id in shown)
    fact_lines = "\n".join(f"- {f['text']}" for f in facts)
    return f"{body}\n\n## Personal Context\n{fact_lines}" if fact_lines else body


# --------------------------------------------------------------------------- #
# Part 1


def part1(
    cases: list[dict[str, Any]], labels: dict[str, dict[str, Any]], sections: list[Item], vecs: dict[str, list[float]]
) -> dict[str, Any]:
    by_id = {s.id: s for s in sections}
    core_tokens = sum(tokens(by_id[c].text) for c in CORE if c in by_id)
    full_tokens = tokens("\n\n".join(s.text for s in sections)[:12_000])
    grid = {}
    query_vecs = encode([c["prompt"][:2000] for c in cases])
    for margin in (0.0, 0.02, 0.05, 0.08):
        for floor in (0.5, 0.55, 0.6, 0.65, 0.7):
            added_tokens, hit, need = [], 0, 0
            for case, qvec in zip(cases, query_vecs, strict=True):
                picked = similar_sections(qvec, sections, vecs, margin, floor)
                added_tokens.append(sum(tokens(by_id[p].text) for p in picked))
                ref = labels.get(case["run_id"], {}).get("reference", {})
                used = {k for k, v in ref.items() if v and k.startswith("profile:") and k not in CORE}
                need += len(used)
                hit += len(used & set(picked))
            grid[f"margin={margin},floor={floor}"] = {
                "mean_profile_tokens": round(core_tokens + statistics.mean(added_tokens)),
                "used_noncore_sections_given": f"{hit}/{need}",
                "recall": round(hit / need, 3) if need else None,
            }
    used_core = used_noncore = 0
    for row in labels.values():
        for k, v in row["reference"].items():
            if v and k.startswith("profile:"):
                used_core += k in CORE
                used_noncore += k not in CORE
    return {
        "full_profile_tokens": full_tokens,
        "core_tokens": core_tokens,
        "reference_used_profile_sections": {"core": used_core, "non_core": used_noncore},
        "core_sim_grid": grid,
    }


# --------------------------------------------------------------------------- #
# Part 2


async def questions(
    cases: list[dict[str, Any]], labels: dict[str, dict[str, Any]], sections: list[Item]
) -> list[dict[str, Any]]:
    rng = random.Random(86)
    out: list[dict[str, Any]] = []
    real = [
        c
        for c in cases
        if c["kind"] == "chat"
        and any(
            v and k.startswith("profile:") and k not in CORE
            for k, v in labels.get(c["run_id"], {}).get("reference", {}).items()
        )
    ]
    rng.shuffle(real)
    out += [{"source": "real_user", "question": c["prompt"][:2000], "case": c} for c in real[:REAL_QUESTIONS]]
    for q in (
        "Who am I?",
        "What do you know about me?",
        "What are my main goals right now?",
        "Summarize what I am working on these days.",
        "What should I focus on this week, given what you know about me?",
    ):
        out.append({"source": "broad", "question": q, "case": None})
    for section in sections:
        if section.id in CORE or len(section.text) < 200:
            continue
        made = await ask_json(
            "claude-sonnet-5-5",
            "Write one natural question that the user might ask their assistant, which can be answered only "
            "with facts from this section of their profile. Do not copy facts into the question.",
            section.text,
            QUESTION_SCHEMA,
        )
        out.append({"source": f"section:{section.id}", "question": made["data"]["question"], "case": None})
    labelled = set(labels)
    other = [
        c
        for c in cases
        if c["run_id"] in labelled
        and not any(v and k.startswith("profile:") for k, v in labels[c["run_id"]]["reference"].items())
    ]
    rng.shuffle(other)
    out += [{"source": "not_about_user", "question": c["prompt"][:2000], "case": c} for c in other[:OTHER_TASKS]]
    return out


async def answer(
    variant: str,
    question: dict[str, Any],
    memory: Any,
    sections: list[Item],
    vecs: dict[str, list[float]],
    margin: float,
    floor: float,
) -> dict[str, Any]:
    by_id = {s.id: s for s in sections}
    recalled = await memory.recall(question["question"])
    facts = [{"text": f.text} for f in recalled["facts"]]
    shown = set(CORE)
    if variant in ("core_sim", "both"):
        shown |= set(similar_sections(recalled["qvec"], sections, vecs, margin, floor))
    memory_block = render_memory(sections, shown, facts, full=variant == "full")
    system = f"{SYSTEM}\n\n# Memory\n{memory_block}"
    tools = [READ_PROFILE] if variant in ("core_tool", "both") else []
    messages: list[dict[str, Any]] = [{"role": "user", "content": question["question"]}]
    tool_calls = 0
    start_tokens = tokens(memory_block)
    for _ in range(3):
        turn = await ask_with_tools(ANSWER_MODEL, system, messages, tools)
        uses = [b for b in turn["content"] if b["type"] == "tool_use"]
        if not uses:
            text = "\n".join(b.get("text", "") for b in turn["content"] if b["type"] == "text")
            return {"text": text, "tool_calls": tool_calls, "start_tokens": start_tokens}
        tool_calls += 1
        messages.append({"role": "assistant", "content": turn["content"]})
        results = []
        for use in uses:
            wanted = [w.lower() for w in use.get("input", {}).get("sections", [])]
            picked = [
                s for s in sections if s.id not in shown and (not wanted or any(w in s.id.lower() for w in wanted))
            ]
            if not picked:
                picked = [s for s in sections if s.id not in shown]
            results.append(
                {"type": "tool_result", "tool_use_id": use["id"], "content": "\n\n".join(s.text for s in picked)}
            )
            shown |= {s.id for s in picked}
        messages.append({"role": "user", "content": results})
    del by_id
    return {"text": "(no final answer)", "tool_calls": tool_calls, "start_tokens": start_tokens}


async def grade(question: dict[str, Any], reply: str, full_profile: str) -> dict[str, Any]:
    user = f"## Full profile\n{full_profile}\n\n## Request\n{question['question']}\n\n## Answer\n{reply}"
    result = await ask_json(GRADER_MODEL, GRADER, user, GRADE_SCHEMA, effort="low")
    return result["data"]


async def part2(
    cases: list[dict[str, Any]],
    labels: dict[str, dict[str, Any]],
    sections: list[Item],
    vecs: dict[str, list[float]],
    margin: float,
    floor: float,
) -> dict[str, Any]:
    from experiments.memory_recall.common import MemorySnapshot, profile_text

    memory = MemorySnapshot()
    qs = await questions(cases, labels, sections)
    full_profile = profile_text()
    # core_sim and both were dropped after part 1: similarity gave 2 of 38 used non-core sections.
    variants = ("full", "core", "core_tool")
    rows = []

    async def run(q: dict[str, Any], variant: str) -> dict[str, Any]:
        reply = await answer(variant, q, memory, sections, vecs, margin, floor)
        graded = await grade(q, reply["text"], full_profile)
        return {"source": q["source"].split(":")[0], "variant": variant, **reply, **graded}

    rows = await asyncio.gather(*(run(q, v) for q in qs for v in variants))
    write_jsonl(DATA_DIR / "e86_answers.jsonl", [{k: v for k, v in r.items() if k != "text"} for r in rows])
    summary: dict[str, Any] = {"questions": len(qs), "by_source": {}}
    for source in sorted({r["source"] for r in rows}):
        summary["by_source"][source] = {}
        for variant in variants:
            sel = [r for r in rows if r["source"] == source and r["variant"] == variant]
            summary["by_source"][source][variant] = {
                "n": len(sel),
                "mean_score": round(statistics.mean(r["score"] for r in sel), 3),
                "full_marks": round(sum(r["score"] == 2 for r in sel) / len(sel), 3),
                "invented": sum(r["invented"] for r in sel),
                "tool_call_rate": round(sum(r["tool_calls"] > 0 for r in sel) / len(sel), 3),
                "mean_start_tokens": round(statistics.mean(r["start_tokens"] for r in sel)),
            }
    for variant in variants:
        sel = [r for r in rows if r["variant"] == variant]
        summary.setdefault("all", {})[variant] = {
            "mean_score": round(statistics.mean(r["score"] for r in sel), 3),
            "full_marks": round(sum(r["score"] == 2 for r in sel) / len(sel), 3),
            "invented": sum(r["invented"] for r in sel),
            "tool_call_rate": round(sum(r["tool_calls"] > 0 for r in sel) / len(sel), 3),
        }
    return summary


async def main() -> None:
    cases = read_jsonl(DATA_DIR / "cases.jsonl")
    labels = {r["run_id"]: r for r in read_jsonl(DATA_DIR / "e85_labels.jsonl")}
    sections = profile_sections()
    vecs = section_vectors(sections)
    before = SPEND.usd
    report = {"part1": part1(cases, labels, sections, vecs)}
    print(json.dumps(report["part1"], indent=2))
    if sys.argv[1:] == ["part1"]:
        return
    margin, floor = CHOSEN
    report["chosen_core_sim"] = {"margin": margin, "floor": floor}
    report["part2"] = await part2(cases, labels, sections, vecs, margin, floor)
    report["spend_usd_this_run"] = round(SPEND.usd - before, 2)
    path = write_result("e86_profile", report)
    print(path.read_text())


# Set from the part 1 grid: the cheapest setting that still gives most used sections.
CHOSEN = (0.05, 0.55)


if __name__ == "__main__":
    asyncio.run(main())
