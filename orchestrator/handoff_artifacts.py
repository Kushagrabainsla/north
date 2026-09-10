"""Safe bounded reads of agent handoff artifacts."""

from __future__ import annotations

from pathlib import Path


def primary_artifact_path(produces: list[str], handoff_dir: str) -> Path | None:
    """Resolve the first declared artifact path for a stage, if it has one."""
    if not produces:
        return None
    return Path(produces[0].replace("{handoff_dir}", handoff_dir))


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
