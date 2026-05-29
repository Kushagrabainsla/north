"""File-backed implementation of ContextStore. The v1 default."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING

from context.base import ContextStore
from context.exceptions import ContextReadError, ContextWriteError
from context.models import ContextDocument

if TYPE_CHECKING:
    from context.embedding_index import EmbeddingIndex

_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "and", "or", "but", "not", "i",
    "my", "me", "we", "our", "you", "your", "it", "its",
})


class FileContextStore(ContextStore):
    """The five context documents persist as markdown files under `base_path`.

    `base_path` is created on construction if it does not exist. Every public
    method off-loads blocking file I/O to a thread so callers stay non-blocking
    on the event loop (docs/CODING_STYLE.md Section 10.3).
    """

    def __init__(self, base_path: Path, embedding_index: "EmbeddingIndex | None" = None) -> None:
        self._base_path = base_path
        self._base_path.mkdir(parents=True, exist_ok=True)
        self._embedding_index = embedding_index
        # Per-document locks serialise concurrent append() calls so the
        # read-modify-write inside _append_sync is always atomic from the
        # perspective of async callers.
        self._locks: dict[str, asyncio.Lock] = {}

    def _path(self, document: ContextDocument) -> Path:
        return self._base_path / document.value

    async def read(self, document: ContextDocument) -> str:
        try:
            return await asyncio.to_thread(self._read_sync, document)
        except OSError as e:
            raise ContextReadError(f"Failed to read {document.value}: {e}") from e

    def _read_sync(self, document: ContextDocument) -> str:
        path = self._path(document)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    async def write(self, document: ContextDocument, content: str) -> None:
        try:
            await asyncio.to_thread(self._write_sync, document, content)
        except OSError as e:
            raise ContextWriteError(f"Failed to write {document.value}: {e}") from e
        if self._embedding_index is not None:
            # Invalidate synchronously so any search that runs before the
            # background update completes will reload from DB rather than
            # serving the pre-write cache.
            self._embedding_index.invalidate_cache()
            asyncio.create_task(
                self._embedding_index.update_document(document.value, content)
            )

    def _write_sync(self, document: ContextDocument, content: str) -> None:
        self._path(document).write_text(content, encoding="utf-8")

    async def append(self, document: ContextDocument, delta: str) -> None:
        lock = self._locks.setdefault(document.value, asyncio.Lock())
        async with lock:
            try:
                await asyncio.to_thread(self._append_sync, document, delta)
            except OSError as e:
                raise ContextWriteError(f"Failed to append to {document.value}: {e}") from e
            if self._embedding_index is not None:
                new_content = await self.read(document)
                asyncio.create_task(
                    self._embedding_index.update_document(document.value, new_content)
                )

    def _append_sync(self, document: ContextDocument, delta: str) -> None:
        path = self._path(document)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        separator = "\n" if existing else ""
        path.write_text(f"{existing}{separator}{delta}", encoding="utf-8")

    async def search(self, query: str, max_results: int = 5) -> str:
        if self._embedding_index is not None:
            hits = await self._embedding_index.search(query, max_results=max_results)
            if hits:
                sections = [f"[{label}]\n{chunk}" for label, chunk in hits]
                return "\n\n---\n\n".join(sections)
        return await asyncio.to_thread(self._search_sync, query, max_results)

    def _search_sync(self, query: str, max_results: int) -> str:  # noqa: C901
        query_words = _tokenize(query)
        if not query_words:
            return ""

        hits: list[tuple[int, str, str]] = []  # (score, doc_label, paragraph)

        for doc in ContextDocument:
            path = self._path(doc)
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            label = doc.value.removesuffix(".md").replace("_", " ").title()
            for para in _split_paragraphs(text):
                score = _score(para, query_words)
                if score > 0:
                    hits.append((score, label, para.strip()))

        hits.sort(key=lambda h: h[0], reverse=True)
        top = hits[:max_results]

        if not top:
            return ""

        sections = [f"[{label}]\n{para}" for _, label, para in top]
        return "\n\n---\n\n".join(sections)


def _tokenize(text: str) -> frozenset[str]:
    """Lowercase words, strip punctuation, drop stopwords."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return frozenset(w for w in words if w not in _STOPWORDS)


def _split_paragraphs(text: str) -> list[str]:
    """Split on one or more blank lines; drop empty results."""
    return [p for p in re.split(r"\n{2,}", text) if p.strip()]


def _score(paragraph: str, query_words: frozenset[str]) -> int:
    """Count distinct query words that appear in the paragraph."""
    para_words = _tokenize(paragraph)
    return len(query_words & para_words)
