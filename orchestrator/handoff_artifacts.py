"""Safe bounded reads of agent handoff artifacts."""

from __future__ import annotations

from pathlib import Path


def read_artifact(path: Path | None, max_chars: int) -> str | None:
    """Read and cap an artifact, returning ``None`` when it is absent or empty."""
    if path is None:
        return None
    try:
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text:
        return None
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n[…{len(text) - max_chars} chars truncated]"
    return text
