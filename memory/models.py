"""Models for the unified memory layer. See docs/ARCHITECTURE.md Section 5."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ContextDocument(StrEnum):
    """The five markdown files that constitute north's context layer.

    Members are the file names on disk. Using the enum at API boundaries means
    no caller can ask for an unknown document - the type system rejects it.
    """

    PUBLIC = "public.md"
    PRIVATE = "private.md"
    PRIVACY_RULES = "privacy_rules.md"
    JUDGEMENT_RULES = "judgement_rules.md"
    NORTH_STARS = "north_stars.md"


@dataclass(frozen=True)
class MemoryPrincipal:
    """Who is asking for memory, and what they are permitted to see.

    Built once per caller (agent or tool) by the gateway from privacy_rules.md.
    Every gated read filters against this, so permission lives in one place and
    no caller can over-read by going around the gateway.
    """

    name: str
    domain: str | None
    allowed_documents: frozenset[ContextDocument]
    # Fact categories this principal may read: public / judgement_rules / north_stars / private.
    allowed_categories: frozenset[str]
    # Episode domains this principal may read (its own domain plus a shared set).
    allowed_domains: frozenset[str]
    can_read_private: bool


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
