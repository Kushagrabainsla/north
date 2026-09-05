"""Local implementation of the memory gateway. See docs/ARCHITECTURE.md Section 5.

Owns the only path by which agents, tools, and internal checks read memory.
Two boundaries are enforced here: episodic task history is scoped to a
principal's own domain, and personal facts are scoped to the topics its domain
has a use for. Nothing in the store is secret - the fact scoping is about not
carrying a life story through a task that has no use for one.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import TYPE_CHECKING

from memory.base import ContextStore, MemoryGateway
from memory.models import ContextDocument, MemoryContext, MemoryPrincipal
from utils.prompts import load_prompt

if TYPE_CHECKING:
    from memory.episodic import EpisodicStore
    from memory.facts import FactStore

logger = logging.getLogger(__name__)

# Per-document cap before truncation in the whole-document fallback path.
# ~12k chars is roughly 3k tokens - rich personal context without blowing the
# context window on a single large document.
_MAX_DOCUMENT_CHARS = 12_000

# The document read into agent context when no atomic facts exist yet. The other
# documents are consumed by dedicated checks (judgement filter, north-star) or
# injected as the persona (soul), not dumped into every agent prompt.
_RECALL_DOCS = (ContextDocument.USER,)

# Episode domains every agent may read in addition to its own (open-ended work).
_SHARED_EPISODE_DOMAINS = frozenset({"general"})

# Facts the user stated directly in conversation are filed under their context
# document rather than a topic. They are the highest-signal thing the store holds
# - the user said them, out loud, to north - so no domain is denied them.
_ALWAYS_ALLOWED_FACT_TOPICS = frozenset({"user", "identity"})

# Which fact topics each agent domain may read. A domain absent from this map is
# unrestricted, which keeps a newly added agent working rather than silently
# blind; restricting it is then a deliberate edit here.
#
# `engineering` is the narrow one on purpose. Measured against a real store, a
# coding prompt about a discount bug pulled "the user is familiar with PyTorch"
# and "designed the system to fail closed when checkpoint state cannot be
# verified" - the second matched on the word "fail". Skills and past projects
# read as relevant to a cosine score and are noise to the task, whereas how
# someone likes to work genuinely changes how an agent should behave.
_FACT_TOPICS_BY_DOMAIN: dict[str, frozenset[str]] = {
    "engineering": frozenset({"preferences"}),
    "home": frozenset({"preferences", "schedule"}),
    "wellness": frozenset({"health", "preferences", "schedule"}),
}


@lru_cache(maxsize=1)
def _default_persona() -> str:
    """The shipped persona, used when soul.md has not been customized."""
    try:
        return load_prompt("prompts/soul.md").strip()
    except Exception:
        logger.warning("Default persona prompts/soul.md could not be loaded", exc_info=True)
        return ""


class LocalMemoryGateway(MemoryGateway):
    """Gateway over the local stores: facts, episodes, and context documents."""

    def __init__(
        self,
        context_store: ContextStore,
        fact_store: FactStore | None = None,
        episodic_store: EpisodicStore | None = None,
    ) -> None:
        self._context_store = context_store
        self._fact_store = fact_store
        self._episodic_store = episodic_store

    async def principal_for(self, name: str, domain: str | None = None) -> MemoryPrincipal:
        return MemoryPrincipal(
            name=name,
            domain=domain,
            allowed_domains=self._allowed_episode_domains(domain),
            allowed_fact_topics=self._allowed_fact_topics(domain),
        )

    async def recall(
        self,
        principal: MemoryPrincipal,
        query: str,
        *,
        fact_limit: int = 15,
        episode_limit: int = 3,
    ) -> MemoryContext:
        facts, episodes = await asyncio.gather(
            self._recall_facts(query, fact_limit, principal.allowed_fact_topics),
            self._recall_episodes(principal, query, episode_limit),
        )
        # Whole-document fallback only when no atomic facts are available - and
        # only for a principal allowed to read every topic. The document is the
        # *whole* profile, so serving it to a topic-scoped caller would hand back
        # everything the scoping just excluded, by a different route.
        unrestricted = principal.allowed_fact_topics is None
        documents = [] if facts or not unrestricted else await self._read_documents()
        return MemoryContext(facts=facts, episodes=episodes, documents=documents)

    async def read_document(self, doc: ContextDocument) -> str:
        return await self._safe_read(doc)

    async def read_persona(self) -> str:
        return (await self._safe_read(ContextDocument.SOUL)).strip() or _default_persona()

    # ------------------------------------------------------------------ #

    async def _recall_facts(
        self, query: str, limit: int, allowed_topics: frozenset[str] | None = None
    ) -> list[str]:
        if self._fact_store is None:
            return []
        try:
            # No count() pre-check: search() already returns [] on an empty store,
            # and the extra round trip ran on every agent's recall path.
            return await self._fact_store.search(query, max_results=limit, allowed_categories=allowed_topics)
        except Exception:
            logger.warning("MemoryGateway: fact search failed", exc_info=True)
            return []

    async def _recall_episodes(self, principal: MemoryPrincipal, query: str, limit: int) -> list[str]:
        if self._episodic_store is None or not principal.allowed_domains:
            return []
        try:
            return await self._episodic_store.search(
                query, max_results=limit, allowed_domains=principal.allowed_domains
            )
        except Exception:
            logger.warning("MemoryGateway: episodic search failed", exc_info=True)
            return []

    async def _read_documents(self) -> list[str]:
        """Read the whole-document fallback set concurrently, truncating large ones."""
        raw_parts = await asyncio.gather(*(self._safe_read(doc) for doc in _RECALL_DOCS))
        out: list[str] = []
        for text in raw_parts:
            if not text:
                continue
            if len(text) > _MAX_DOCUMENT_CHARS:
                omitted = len(text) - _MAX_DOCUMENT_CHARS
                text = text[:_MAX_DOCUMENT_CHARS] + f"\n\n[…{omitted} chars omitted - document too large]"
            out.append(text)
        return out

    async def _safe_read(self, doc: ContextDocument) -> str:
        try:
            return await self._context_store.read(doc) or ""
        except Exception:
            logger.warning("MemoryGateway: failed to read %s", doc, exc_info=True)
            return ""

    @staticmethod
    def _allowed_fact_topics(domain: str | None) -> frozenset[str] | None:
        """Fact topics this domain may read, or None for no restriction.

        A system principal (no domain) is unrestricted: it is north asking about
        the user on the user's behalf, which is the case that needs everything.
        """
        if domain is None:
            return None
        scoped = _FACT_TOPICS_BY_DOMAIN.get(domain)
        if scoped is None:
            return None
        return scoped | _ALWAYS_ALLOWED_FACT_TOPICS

    @staticmethod
    def _allowed_episode_domains(domain: str | None) -> frozenset[str]:
        """Episodes a principal may read: its own domain plus the shared set.

        A system principal (no domain) recalls no episodes.
        """
        if domain is None:
            return frozenset()
        return frozenset({domain}) | _SHARED_EPISODE_DOMAINS
