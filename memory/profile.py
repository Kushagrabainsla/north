"""Your profile: the readable summary agents get, built from your facts and your documents.

One home for each thing North knows. Facts about you live in the fact store; your
judgement rules and north stars live in their documents, which you edit on the
Memory page and which the approval decider and the goal check read. The profile
(user.md) is a view of both, rewritten whenever either changes, so nothing is
kept twice and an edit in one place cannot leave a stale copy in another.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from memory.dedup import normalize_for_dedup
from memory.models import ContextDocument

if TYPE_CHECKING:
    from memory.base import ContextStore
    from memory.facts import FactStore

logger = logging.getLogger(__name__)

# Identity first: a profile is read top-down, and who someone is comes before
# what they are working on.
PROFILE_TOPIC_ORDER = [
    "identity",
    "preferences",
    "jobs",
    "education",
    "projects",
    "skills",
    "schedule",
    "health",
    "finances",
    "other",
]
_ALWAYS_FULL = frozenset({"identity", "preferences"})

# The documents the profile shows as written, after the facts.
_DOCUMENT_SECTIONS = (
    (ContextDocument.NORTH_STARS, "North stars (your goals)"),
    (ContextDocument.JUDGEMENT_RULES, "Judgement rules"),
)
# Fact categories that used to hold copies of those documents.
_COPIED_INTO_FACTS = {
    "north_stars": ContextDocument.NORTH_STARS,
    "judgement_rules": ContextDocument.JUDGEMENT_RULES,
}


async def rebuild_profile(fact_store: FactStore | None, context_store: ContextStore) -> None:
    """Rewrite user.md from your facts and your goal and rule documents. Never raises."""
    if fact_store is None:
        return  # without facts this would overwrite the profile with only the documents
    try:
        profile = await fact_store.render_profile(topic_order=PROFILE_TOPIC_ORDER, always_full=_ALWAYS_FULL)
        sections = []
        for document, title in _DOCUMENT_SECTIONS:
            text = (await context_store.read(document)).strip()
            if text:
                sections.append(f"## {title}\n{text}")
        if profile or sections:
            await context_store.write(ContextDocument.USER, "\n\n".join([profile or "# User Profile", *sections]))
    except Exception:
        logger.warning("Could not rebuild the user profile document", exc_info=True)


async def move_rule_and_goal_facts(fact_store: FactStore, context_store: ContextStore) -> int:
    """Move judgement-rule and north-star facts into their documents, once; return how many.

    Extraction used to write each rule and goal twice: into its document, which
    the decider, the goal check and the Documents tab read, and into the fact
    store, which the profile and recall read. Editing the document changed one
    copy. Anything only the fact store held is added to the document first, so
    nothing is lost; a fact that has ended or was replaced is not brought back.
    """
    moved = 0
    for category, document in _COPIED_INTO_FACTS.items():
        facts = await fact_store.all_facts(category)
        if not facts:
            continue
        existing = await context_store.read(document)
        known = {normalize_for_dedup(line) for line in existing.splitlines() if line.strip()}
        missing = [
            fact["content"]
            for fact in facts
            if fact["status"] == "active" and not fact["ended"] and normalize_for_dedup(fact["content"]) not in known
        ]
        if missing:
            await context_store.write(document, "\n".join([existing.rstrip(), *missing]).strip() + "\n")
        for fact in facts:
            await fact_store.delete_fact(fact["id"])
        moved += len(facts)
    if moved:
        logger.info("Moved %d judgement-rule and north-star fact(s) into their documents", moved)
    return moved
