"""Models for the unified memory layer. See docs/ARCHITECTURE.md Section 5."""

from __future__ import annotations

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


@dataclass(frozen=True)
class MemoryContext:
    """The gated, merged result of a recall, ready to render into a prompt."""

    facts: list[str] = field(default_factory=list)
    episodes: list[str] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)

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
