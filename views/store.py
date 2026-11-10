"""Where views live: one JSON file each in north's home, and the drafts waiting for your OK.

A view is written as a draft first, so you can open it before saying yes; approving the
card that asks to add it moves the draft onto the Views page. Deleting one removes its
file. Nothing else writes here.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from views.document import SLUG


class ViewStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._drafts = root / "drafts"

    def _path(self, view_id: str, *, draft: bool) -> Path:
        # An id from a URL or a model is a file name here: only a plain slug can be one.
        if not re.fullmatch(SLUG, view_id):
            raise ValueError(f"{view_id!r} is not a view id: lowercase letters, digits and dashes.")
        return (self._drafts if draft else self._root) / f"{view_id}.json"

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return document if isinstance(document, dict) else None

    def _write(self, path: Path, document: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written whole, then renamed: a view is never seen half-written.
        handle, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(document, file, indent=2, ensure_ascii=False)
        os.replace(temporary, path)

    def all(self) -> list[dict[str, Any]]:
        """Every live view, newest change first."""
        if not self._root.is_dir():
            return []
        views = [view for path in self._root.glob("*.json") if (view := self._read(path))]
        return sorted(views, key=lambda view: view.get("updated_at", ""), reverse=True)

    def get(self, view_id: str, *, draft: bool = False) -> dict[str, Any] | None:
        return self._read(self._path(view_id, draft=draft))

    def save_draft(self, document: dict[str, Any]) -> None:
        self._write(self._path(document["id"], draft=True), {**document, "updated_at": _now()})

    def publish(self, view_id: str) -> dict[str, Any] | None:
        """Put the draft of *view_id* on the Views page; None when there is no draft."""
        draft = self.get(view_id, draft=True)
        if draft is None:
            return None
        live = {**draft, "updated_at": _now()}
        self._write(self._path(view_id, draft=False), live)
        self._path(view_id, draft=True).unlink(missing_ok=True)
        return live

    def delete(self, view_id: str) -> bool:
        path = self._path(view_id, draft=False)
        if not path.exists():
            return False
        path.unlink()
        return True


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
