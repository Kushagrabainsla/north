"""Before and after the #86 change, measured with the product code on a copy of the snapshot.

Before: the profile as north has it now, cut at 12,000 characters, and every recalled fact.
After:  the profile rebuilt with the core sections, goals and rules first, and recall
        that skips a fact the profile already shows.

Run: .venv/bin/python -m experiments.memory_recall.after_86
"""

from __future__ import annotations

import asyncio
import shutil
import statistics

from experiments.memory_recall.common import DATA_DIR, SNAPSHOT_DIR, embed_fn, read_jsonl, tokens, write_result
from memory import LocalMemoryGateway, SQLiteContextStore
from memory.facts import FactStore
from memory.models import ContextDocument, document_sections
from memory.profile import rebuild_profile

CAP = 12_000


def cut_sections(profile: str) -> dict[str, int]:
    """Characters of each section that the 12,000-character cap removes."""
    out, start = {}, 0
    for heading, text in document_sections(profile):
        start = profile.find(text, start)
        lost = max(0, start + len(text) - max(CAP, start))
        if lost:
            out[heading] = min(lost, len(text))
        start += len(text)
    return out


async def main() -> None:
    work = DATA_DIR / "after_86"
    work.mkdir(parents=True, exist_ok=True)
    for name in ("facts.db", "memory.db"):
        shutil.copy(SNAPSHOT_DIR / name, work / name)
    context = SQLiteContextStore(work / "memory.db")
    facts = FactStore(db_path=work / "facts.db", embed_fn=embed_fn)
    before_profile = await context.read(ContextDocument.USER)
    await rebuild_profile(facts, context)
    after_profile = await context.read(ContextDocument.USER)

    gateway = LocalMemoryGateway(context, facts)
    principal = await gateway.principal_for("north", "general")
    cases = read_jsonl(DATA_DIR / "cases.jsonl")
    before_tokens, after_tokens, dropped, recalled_total = [], [], 0, 0
    old_shown = before_profile[:CAP]
    for case in cases:
        all_facts = await facts.search(case["prompt"])
        recalled = await gateway.recall(principal, case["prompt"])
        recalled_total += len(all_facts)
        dropped += len(all_facts) - len(recalled.facts)
        before_tokens.append(tokens(old_shown) + sum(tokens(f) for f in all_facts))
        after_tokens.append(tokens(recalled.render()) - sum(tokens(e) for e in recalled.episodes))
    already_in_old = 0
    for case in cases[:50]:
        already_in_old += sum(f"- {f}" in old_shown for f in await facts.search(case["prompt"]))
    report = {
        "profile_chars": {"before": len(before_profile), "after": len(after_profile)},
        "cut_by_cap": {"before": cut_sections(before_profile), "after": cut_sections(after_profile)},
        "recalled_facts": recalled_total,
        "facts_skipped_as_already_in_profile": dropped,
        "skipped_share": round(dropped / recalled_total, 3) if recalled_total else 0.0,
        "facts_shown_by_old_profile_first_50_tasks": already_in_old,
        "memory_tokens_per_task_without_notes": {
            "before": round(statistics.mean(before_tokens)),
            "after": round(statistics.mean(after_tokens)),
        },
    }
    write_result("after_86", report)
    print(report)


if __name__ == "__main__":
    asyncio.run(main())
