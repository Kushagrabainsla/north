"""Context-layer exceptions. See docs/CODING_STYLE.md Section 13.1."""

from __future__ import annotations

from exceptions import NorthError


class ContextError(NorthError):
    """Base class for context-layer failures."""


class ContextReadError(ContextError):
    """Raised when reading a context document fails."""


class ContextWriteError(ContextError):
    """Raised when writing or appending to a context document fails."""
