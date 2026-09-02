"""Local implementation of the memory gateway. See docs/ARCHITECTURE.md Section 5.

Owns the only path by which agents, tools, and internal checks read memory.
Context documents and facts are non-sensitive; the one boundary the gateway
enforces is episodic task history, scoped to a principal's own domain.
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
        return MemoryPrincipal(name=name, domain=domain, allowed_domains=self._allowed_episode_domains(domain))

    async def recall(
        self,
        principal: MemoryPrincipal,
        query: str,
        *,
        fact_limit: int = 15,
        episode_limit: int = 3,
    ) -> MemoryContext:
        facts, episodes = await asyncio.gather(
            self._recall_facts(query, fact_limit),
            self._recall_episodes(principal, query, episode_limit),
        )
        # Whole-document fallback only when no atomic facts are available.
        documents = [] if facts else await self._read_documents()
        return MemoryContext(facts=facts, episodes=episodes, documents=documents)

    async def read_document(self, doc: ContextDocument) -> str:
        return await self._safe_read(doc)

    async def read_persona(self) -> str:
        return (await self._safe_read(ContextDocument.SOUL)).strip() or _default_persona()

    # ------------------------------------------------------------------ #

    async def _recall_facts(self, query: str, limit: int) -> list[str]:
        if self._fact_store is None:
            return []
        try:
            # No count() pre-check: search() already returns [] on an empty store,
            # and the extra round trip ran on every agent's recall path.
            return await self._fact_store.search(query, max_results=limit)
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
    def _allowed_episode_domains(domain: str | None) -> frozenset[str]:
        """Episodes a principal may read: its own domain plus the shared set.

        A system principal (no domain) recalls no episodes.
        """
        if domain is None:
            return frozenset()
        return frozenset({domain}) | _SHARED_EPISODE_DOMAINS
