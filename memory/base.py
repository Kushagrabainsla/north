"""Memory layer interfaces. See docs/ARCHITECTURE.md Section 5.

Holds the two base ABCs: ``ContextStore`` (the five markdown documents) and
``MemoryGateway`` (the single gated read path over facts, episodes, and
documents). Each gateway call carries a ``MemoryPrincipal`` so per-caller
permissions are enforced in one place and nothing can bypass the gate by
talking to a store directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from memory.models import ContextDocument, MemoryContext, MemoryPrincipal

if TYPE_CHECKING:
    from memory.embeddings import EmbeddingIndex


class ContextStore(ABC):
    """Read and write the five context documents.

    `read`, `write`, and `append` are mandatory. `search()` is reserved for a
    future `DBContextStore` backed by a vector DB; v1's `FileContextStore`
    inherits the default that raises `NotImplementedError` so any accidental
    v1 caller fails loudly rather than silently returning empty results.
    """

    @abstractmethod
    async def read(self, document: ContextDocument) -> str:
        """Return the full text of `document`. A document that has never been
        written reads as an empty string, not an error."""

    @abstractmethod
    async def write(self, document: ContextDocument, content: str) -> None:
        """Overwrite `document` with `content`."""

    @abstractmethod
    async def append(self, document: ContextDocument, delta: str) -> None:
        """Append `delta` to `document`, separated from existing content by a
        single newline. If the document does not exist, create it."""

    def attach_embedding_index(self, index: EmbeddingIndex) -> None:  # noqa: ARG002, B027
        """Attach a semantic embedding index for use in search().

        Called after construction when the embed function is available.
        Subclasses that support embedding override this method.
        """

    async def search(self, query: str, max_results: int = 5) -> str:
        """Keyword search across all context documents.

        Returns the top `max_results` paragraphs ranked by query-word overlap,
        each labelled with its source document. Returns an empty string when
        nothing matches.
        """
        raise NotImplementedError("search() must be implemented by a concrete ContextStore subclass.")


class MemoryGateway(ABC):
    """Single, gated entry point for every read of north's memory."""

    @property
    @abstractmethod
    def system_principal(self) -> MemoryPrincipal:
        """Principal for internal system reads (judgement filter, north-star check).

        Grants the non-sensitive system documents (public, judgement_rules,
        north_stars); never private, and recalls no episodes.
        """
        ...

    @abstractmethod
    async def principal_for(self, name: str, domain: str | None = None) -> MemoryPrincipal:
        """Resolve a caller's permissions from privacy_rules.md into a principal."""
        ...

    @abstractmethod
    async def recall(
        self,
        principal: MemoryPrincipal,
        query: str,
        *,
        fact_limit: int = 15,
        episode_limit: int = 3,
    ) -> MemoryContext:
        """Return the gated, merged memory relevant to *query* for *principal*.

        Filters facts by allowed category, episodes by allowed domain, and falls
        back to the permitted context documents when no facts exist yet.
        """
        ...

    @abstractmethod
    async def read_document(self, principal: MemoryPrincipal, doc: ContextDocument) -> str:
        """Return a full context document, or '' if the principal may not read it."""
        ...
