"""Abstract interface for the Context Layer. See README Section 5.2."""

from __future__ import annotations

from abc import ABC, abstractmethod

from context.models import ContextDocument


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

    def search(self, query: str) -> str:
        """Semantic search. Not implemented in v1.

        Upgrade to `DBContextStore` when context files outgrow LLM context
        windows. Until then, this raises loudly — see docs/CODING_STYLE.md
        Section 6.2.
        """
        raise NotImplementedError(
            "search() requires DBContextStore. "
            "FileContextStore does not support semantic search."
        )
