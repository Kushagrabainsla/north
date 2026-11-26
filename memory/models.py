"""Models for the unified memory layer. See docs/ARCHITECTURE.md Section 5."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class ContextDocument(StrEnum):
    """The markdown documents that make up north's context layer.

    Members are the file names on disk. Using the enum at API boundaries means
    no caller can ask for an unknown document - the type system rejects it.

    user/judgement_rules/north_stars hold the user's own data, decision
    preferences, and goals; soul is north's persona. All are non-sensitive and
    readable by every caller.
    """

    USER = "user.md"
    JUDGEMENT_RULES = "judgement_rules.md"
    NORTH_STARS = "north_stars.md"
    SOUL = "soul.md"


@dataclass(frozen=True)
class MemoryPrincipal:
    """Who is asking for memory, and which of it they may read.

    Two boundaries. Episodic task history is scoped by domain: an agent sees its
    own domain's episodes plus a shared set, never another domain's.

    Facts are scoped by *topic*, which is about relevance rather than secrecy.
    Nothing here is sensitive, but a coding agent handed the user's tax figures
    is carrying noise through every turn of its loop - and measurably was:
    engineering prompts pulled 4 personal facts on average against 5 for
    questions actually about the user, so the similarity search was not
    discriminating at all. Which facts a task wants is a property of the task.
    """

    name: str
    domain: str | None
    # Episode domains this principal may read (its own domain plus a shared set).
    allowed_domains: frozenset[str]
    # Fact topics this principal may read. None means no restriction.
    allowed_fact_topics: frozenset[str] | None = None
    project_id: str = ""
    workspace_id: str = ""


@dataclass(frozen=True)
class MemoryItem:
    """One recalled thing, with the id that its use is recorded under (#85)."""

    id: str  # "fact:<id>", "episode:<id>" or "profile:<section heading>"
    kind: str  # fact | episode | profile
    text: str


def document_sections(text: str) -> list[tuple[str, str]]:
    """Split a markdown document at its '## ' headings: (heading, section text) pairs.

    The profile is one document, but its sections are used very differently: in
    120 replayed tasks the judgement rules were used in 58, health in 1. So the
    use of each section is recorded on its own.
    """
    out: list[tuple[str, str]] = []
    for chunk in re.split(r"(?m)^(?=## )", text):
        if chunk.strip():
            out.append((chunk.strip().splitlines()[0].lstrip("# ").strip(), chunk.strip()))
    return out


@dataclass(frozen=True)
class MemoryContext:
    """The gated, merged result of a recall, ready to render into a prompt."""

    facts: list[str] = field(default_factory=list)
    episodes: list[str] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)
    # The store ids of `facts` and `episodes`, in the same order. Empty when the
    # recall came from somewhere that has no ids (a test double, an old gateway).
    fact_ids: list[str] = field(default_factory=list)
    episode_ids: list[str] = field(default_factory=list)

    def items(self) -> list[MemoryItem]:
        """Every recalled thing as an item with an id, for recording which ones a task used."""
        out: list[MemoryItem] = []
        for index, text in enumerate(self.facts):
            fid = self.fact_ids[index] if index < len(self.fact_ids) else f"#{index}"
            out.append(MemoryItem(f"fact:{fid}", "fact", text))
        for index, text in enumerate(self.episodes):
            eid = self.episode_ids[index] if index < len(self.episode_ids) else f"#{index}"
            out.append(MemoryItem(f"episode:{eid}", "episode", text))
        for doc_index, document in enumerate(self.documents):
            for index, (heading, section) in enumerate(document_sections(document)):
                # A heading names the section; text with no heading is content and stays out of the id.
                name = heading if section.startswith("#") else f"#{doc_index}.{index}"
                out.append(MemoryItem(f"profile:{name}", "profile", section))
        return out

    def telemetry(self) -> dict[str, object]:
        """Content-free retrieval measurements safe for logs and dashboards."""
        groups = {
            "facts": self.facts,
            "episodes": self.episodes,
            "documents": self.documents,
        }
        counts = {name: len(items) for name, items in groups.items()}
        characters = {name: sum(len(item) for item in items) for name, items in groups.items()}
        total_characters = sum(characters.values())
        return {
            "source_categories": [name for name, count in counts.items() if count],
            "source_counts": counts,
            "source_characters": characters,
            "total_items": sum(counts.values()),
            "total_characters": total_characters,
            "estimated_tokens": (total_characters + 3) // 4,
            # Ids only, never text: which items this task was given (#85).
            "item_ids": [item.id for item in self.items()],
        }

    def render(self) -> str:
        """Format the retrieved memory into one prompt block.

        Facts and documents are mutually exclusive: facts are the primary path,
        documents are the fallback used when no facts exist yet. Episodes are
        appended last, already labelled with their outcome.
        """
        blocks: list[str] = list(self.documents)
        if self.facts:
            blocks.append("## Personal Context\n" + "\n".join(f"- {f}" for f in self.facts))
        if self.episodes:
            blocks.append("## Relevant past context\n" + "\n".join(f"- {e}" for e in self.episodes))
        return "\n\n".join(b for b in blocks if b)
