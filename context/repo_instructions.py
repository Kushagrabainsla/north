"""Discover and load a repository's own coding conventions.

Coding agents follow a repo better when they read its house rules. This loader
finds the well-known instruction files (AGENTS.md, CLAUDE.md, Copilot/Cursor
rules) at the workspace root and the enclosing git root, and returns them as a
single context section the agent can read. Files are small, so they are read
fresh each call — no cache layer (see CODING_STYLE §22).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

# Well-known instruction filenames, in the order they are concatenated.
_INSTRUCTION_FILES: tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    ".github/copilot-instructions.md",
    ".cursorrules",
)
_MAX_INSTRUCTION_CHARS = 12_000


async def load_repo_instructions(workspace: str) -> str:
    """Return the repo's instruction files under *workspace*, merged into one section.

    Searches the workspace root and its enclosing git root. Returns an empty
    string when *workspace* is unset/invalid or no instruction files exist.
    """
    if not workspace:
        return ""
    return await asyncio.to_thread(_load_sync, workspace)


def _git_root(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return None


def _load_sync(workspace: str) -> str:
    try:
        root = Path(workspace).expanduser().resolve()
    except OSError:
        return ""
    if not root.is_dir():
        return ""

    roots: list[Path] = [root]
    git_root = _git_root(root)
    if git_root is not None and git_root != root:
        roots.append(git_root)

    sections: list[str] = []
    seen: set[Path] = set()
    for base in roots:
        for relative in _INSTRUCTION_FILES:
            path = base / relative
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            try:
                text = path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                continue
            if text:
                sections.append(f"### {path.name}\n{text}")

    if not sections:
        return ""
    merged = "## Repository conventions\n" + "\n\n".join(sections)
    if len(merged) > _MAX_INSTRUCTION_CHARS:
        merged = merged[:_MAX_INSTRUCTION_CHARS] + "\n\n[… repo conventions truncated]"
    return merged
