"""Abstract interface for the Context Layer. See README Section 5.2."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from context.models import ContextDocument

if TYPE_CHECKING:
    from context.embedding_index import EmbeddingIndex


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
        raise NotImplementedError(
            "search() must be implemented by a concrete ContextStore subclass."
        )
