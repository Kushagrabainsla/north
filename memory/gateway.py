"""Local implementation of the memory gateway. See docs/ARCHITECTURE.md Section 5.

Owns the only path by which agents, tools, and internal checks read memory, so
the per-caller permission gate lives in one place and nothing can bypass it by
talking to a store directly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from memory.base import ContextStore, MemoryGateway
from memory.models import ContextDocument, MemoryContext, MemoryPrincipal

if TYPE_CHECKING:
    from memory.episodic import EpisodicStore
    from memory.facts import FactStore

logger = logging.getLogger(__name__)

# Per-document cap before truncation in the whole-document fallback path.
# ~12k chars is roughly 3k tokens - rich personal context without blowing the
# context window on a single large document.
_MAX_DOCUMENT_CHARS = 12_000

# Safe defaults when privacy_rules.md has no rule for an agent. Engineering
# agents also read north_stars: design and implementation should be checked
# against long-term goals. private.md is never a default - only an explicit grant.
_ENGINEERING_DEFAULT = frozenset(
    {ContextDocument.PUBLIC, ContextDocument.JUDGEMENT_RULES, ContextDocument.NORTH_STARS}
)
_DEFAULT_DOCS = frozenset({ContextDocument.PUBLIC, ContextDocument.JUDGEMENT_RULES})

# Non-sensitive documents an internal system principal (judgement filter,
# north-star check) may read. Never includes private.md.
_SYSTEM_DOCS = frozenset(
    {ContextDocument.PUBLIC, ContextDocument.JUDGEMENT_RULES, ContextDocument.NORTH_STARS}
)

# Stable, sensible read order for the whole-document fallback.
_DOC_ORDER = (
    ContextDocument.PUBLIC,
    ContextDocument.PRIVATE,
    ContextDocument.JUDGEMENT_RULES,
    ContextDocument.NORTH_STARS,
    ContextDocument.PRIVACY_RULES,
)

# Episode domains every agent may read in addition to its own (open-ended work).
_SHARED_EPISODE_DOMAINS = frozenset({"general"})


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
        self._system_principal = MemoryPrincipal(
            name="system",
            domain=None,
            allowed_documents=_SYSTEM_DOCS,
            allowed_categories=frozenset(doc.value.removesuffix(".md") for doc in _SYSTEM_DOCS),
            allowed_domains=frozenset(),
            can_read_private=False,
        )

    @property
    def system_principal(self) -> MemoryPrincipal:
        return self._system_principal

    async def principal_for(self, name: str, domain: str | None = None) -> MemoryPrincipal:
        allowed_docs = await self._allowed_documents(name, domain)
        # A fact's category is its source document name without the .md suffix.
        allowed_categories = frozenset(doc.value.removesuffix(".md") for doc in allowed_docs)
        return MemoryPrincipal(
            name=name,
            domain=domain,
            allowed_documents=allowed_docs,
            allowed_categories=allowed_categories,
            allowed_domains=self._allowed_episode_domains(domain),
            can_read_private=ContextDocument.PRIVATE in allowed_docs,
        )

    async def recall(
        self,
        principal: MemoryPrincipal,
        query: str,
        *,
        fact_limit: int = 15,
        episode_limit: int = 3,
    ) -> MemoryContext:
        facts = await self._recall_facts(principal, query, fact_limit)
        # Whole-document fallback only when no atomic facts are available.
        documents = [] if facts else await self._read_documents(principal)
        episodes = await self._recall_episodes(principal, query, episode_limit)
        return MemoryContext(facts=facts, episodes=episodes, documents=documents)

    async def read_document(self, principal: MemoryPrincipal, doc: ContextDocument) -> str:
        if doc not in principal.allowed_documents:
            return ""
        return await self._safe_read(doc)

    # ------------------------------------------------------------------ #

    async def _recall_facts(self, principal: MemoryPrincipal, query: str, limit: int) -> list[str]:
        if self._fact_store is None:
            return []
        try:
            if await self._fact_store.count() <= 0:
                return []
            return await self._fact_store.search(
                query, max_results=limit, allowed_categories=principal.allowed_categories
            )
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

    async def _read_documents(self, principal: MemoryPrincipal) -> list[str]:
        """Read the principal's permitted documents concurrently, truncating large ones."""
        docs = [d for d in _DOC_ORDER if d in principal.allowed_documents]
        if not docs:
            return []
        raw_parts = await asyncio.gather(*(self._safe_read(doc) for doc in docs))
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

    async def _allowed_documents(self, name: str, domain: str | None) -> frozenset[ContextDocument]:
        """Resolve which context documents an agent may read from privacy_rules.md.

        Parses lines of the form ``<agent>: can_read: <doc>, <doc>``. Falls back
        to a safe default when the file is missing, empty, or has no rule for
        this agent. private.md is included only when explicitly granted.
        """
        default = _ENGINEERING_DEFAULT if domain == "engineering" else _DEFAULT_DOCS
        try:
            rules_text = await self._context_store.read(ContextDocument.PRIVACY_RULES)
        except Exception:
            return default
        if not rules_text or not rules_text.strip():
            return default
        for line in rules_text.splitlines():
            line = line.strip()
            if not line.startswith(f"{name}:") or "can_read:" not in line:
                continue
            after = line.split("can_read:", 1)[1]
            docs: set[ContextDocument] = set()
            for token in after.split(","):
                token = token.strip()
                try:
                    docs.add(ContextDocument(token))
                except ValueError:
                    logger.debug("privacy_rules.md: unknown document %r for agent %s - skipping", token, name)
            return frozenset(docs) if docs else default
        return default

    @staticmethod
    def _allowed_episode_domains(domain: str | None) -> frozenset[str]:
        """Episodes a principal may read: its own domain plus the shared set.

        A system principal (no domain) recalls no episodes.
        """
        if domain is None:
            return frozenset()
        return frozenset({domain}) | _SHARED_EPISODE_DOMAINS
