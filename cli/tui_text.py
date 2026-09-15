"""Pure text rendering and slash-input parsing for the terminal UI."""

from __future__ import annotations


def estimated_tokens(text: str) -> int:
    """Approximate the session meter's token count at four characters per token."""
    return max(1, len(text) // 4)


def slash_argument(text: str) -> str | None:
    """Return a slash command's first argument, or ``None`` when given bare."""
    parts = text.split()
    return parts[1] if len(parts) > 1 else None


def requested_context_document(text: str) -> str:
    """Return the document named by ``/context <doc>`` or ``/context show <doc>``."""
    parts = text.split()
    if len(parts) == 2 and parts[1] not in ("show", "edit"):
        return parts[1].removesuffix(".md")
    if len(parts) >= 3 and parts[1] == "show":
        return parts[2].removesuffix(".md")
    return ""
