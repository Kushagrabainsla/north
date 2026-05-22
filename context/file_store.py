"""File-backed implementation of ContextStore. The v1 default."""

from __future__ import annotations

import asyncio
from pathlib import Path

from context.base import ContextStore
from context.exceptions import ContextReadError, ContextWriteError
from context.models import ContextDocument


class FileContextStore(ContextStore):
    """The five context documents persist as markdown files under `base_path`.

    `base_path` is created on construction if it does not exist. Every public
    method off-loads blocking file I/O to a thread so callers stay non-blocking
    on the event loop (docs/CODING_STYLE.md Section 10.3).
    """

    def __init__(self, base_path: Path) -> None:
        self._base_path = base_path
        self._base_path.mkdir(parents=True, exist_ok=True)

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

    def _write_sync(self, document: ContextDocument, content: str) -> None:
        self._path(document).write_text(content, encoding="utf-8")

    async def append(self, document: ContextDocument, delta: str) -> None:
        try:
            await asyncio.to_thread(self._append_sync, document, delta)
        except OSError as e:
            raise ContextWriteError(f"Failed to append to {document.value}: {e}") from e

    def _append_sync(self, document: ContextDocument, delta: str) -> None:
        path = self._path(document)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        separator = "\n" if existing else ""
        path.write_text(f"{existing}{separator}{delta}", encoding="utf-8")
