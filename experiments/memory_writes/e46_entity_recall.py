"""Issue #46: would recall by the named things a task mentions pick the facts tasks use?

Uses bucket 1's labelled runs (issue #85, ~/.north/experiments/memory_recall): for 120 real
runs, a judge (Opus 5.5) said which of the facts north put in the prompt the answer used.
The facts come from the snapshot those runs were labelled against.

Entities: Haiku 5.5 lists the named things in each fact and in each task prompt (projects,
organizations, people, places, products, accounts, devices, courses), with the same
instruction for both (cached on disk). "The user" is not an entity.

Entity recall keeps a fact when it shares an entity with the prompt. It is compared with what
north put in the prompt (all labelled facts) on two counts: used facts kept, and facts cut.
A fact that only an entity store would find (one north did not put in the prompt) is not
labelled, so this measures precision only. Counts only; the repo gets no fact text.
    .venv/bin/python experiments/memory_writes/e46_entity_recall.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import anthropic

HOME = Path.home() / ".north"
RECALL = HOME / "experiments" / "memory_recall"
CACHE = HOME / "experiments" / "memory_writes" / "cache"
OUT = Path(__file__).resolve().parent / "results"
MODEL = "claude-haiku-5-5"
INSTRUCTION = (
    "List the named things in the text: projects, organizations, companies, schools, people other than "
    "'the user', places, products, tools, accounts, devices, courses. Use each thing's shortest common "
    "name, in lower case. Do not list generic words ('internship', 'resume', 'job') or 'the user'. "
    'Reply with JSON only: {"entities": ["..."]}.\n\nText:\n'
)


def _key() -> str:
    for line in (HOME / ".env").read_text().splitlines():
        if line.startswith("NORTH_ANTHROPIC_API_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise RuntimeError("NORTH_ANTHROPIC_API_KEY is not set")


CLIENT = anthropic.AsyncAnthropic(api_key=_key(), max_retries=4)
LIMIT = asyncio.Semaphore(8)


async def entities(text: str) -> set[str]:
    prompt = INSTRUCTION + text[:4000]
    path = CACHE / ("entities-" + hashlib.sha256(f"{MODEL}\n{prompt}".encode()).hexdigest() + ".json")
    if path.exists():
        return set(json.loads(path.read_text())["entities"])
    async with LIMIT:
        response = await CLIENT.messages.create(
            model=MODEL, max_tokens=2000, messages=[{"role": "user", "content": prompt}]
        )
    raw = next((b.text for b in response.content if b.type == "text"), "{}")
    try:
        found = json.loads(raw[raw.find("{") : raw.rfind("}") + 1]).get("entities") or []
    except json.JSONDecodeError:
        found = []
    names = sorted({str(e).strip().lower() for e in found if str(e).strip()} - {"user", "the user"})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"entities": names}))
    return set(names)


async def main() -> None:
    labels = [json.loads(line) for line in (RECALL / "e85_labels.jsonl").read_text().splitlines() if line.strip()]
    prompts = {}
    for line in (RECALL / "cases_asof.jsonl").read_text().splitlines():
        if line.strip():
            case = json.loads(line)
            prompts[case["run_id"]] = case["prompt"]
    with sqlite3.connect(f"file:{RECALL / 'snapshot' / 'facts.db'}?mode=ro", uri=True) as conn:
        contents = dict(conn.execute("SELECT id, content FROM context_facts").fetchall())

    fact_ids = sorted(
        {k.split(":", 1)[1] for row in labels for k in row["reference"] if k.startswith("fact:")} & set(contents)
    )
    fact_entities = dict(zip(fact_ids, await asyncio.gather(*(entities(contents[i]) for i in fact_ids)), strict=True))
    runs = [row for row in labels if row["run_id"] in prompts]
    prompt_entities = await asyncio.gather(*(entities(prompts[row["run_id"]]) for row in runs))

    injected = used = kept = kept_used = runs_naming_one = runs_with_used = runs_with_used_kept = 0
    for row, named in zip(runs, prompt_entities, strict=True):
        runs_naming_one += bool(named)
        facts = {k.split(":", 1)[1]: v for k, v in row["reference"].items() if k.startswith("fact:")}
        facts = {i: v for i, v in facts.items() if i in fact_entities}
        chosen = {i for i in facts if fact_entities[i] & named}
        injected += len(facts)
        used += sum(facts.values())
        kept += len(chosen)
        kept_used += sum(facts[i] for i in chosen)
        if any(facts.values()):
            runs_with_used += 1
            runs_with_used_kept += all(i in chosen for i, v in facts.items() if v)
    report = {
        "runs": len(runs),
        "runs_whose_prompt_names_an_entity": runs_naming_one,
        "facts_with_an_entity": sum(bool(e) for e in fact_entities.values()),
        "facts_labelled": len(fact_entities),
        "now": {"facts_in_prompts": injected, "used": used},
        "entity_recall": {
            "facts_in_prompts": kept,
            "used_kept": kept_used,
            "used_lost": used - kept_used,
            "cut_share": round(1 - kept / injected, 3) if injected else None,
        },
        "runs_that_used_a_fact": runs_with_used,
        "of_those_all_used_facts_kept": runs_with_used_kept,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "e46_entity_recall.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
