"""Root exception type for north. All custom exceptions inherit from NorthError."""

from __future__ import annotations


class NorthError(Exception):
    """Base class for every exception raised by north code.

    Callers that want to catch any north-specific failure (and let unrelated
    bugs propagate) can `except NorthError`. See docs/CODING_STYLE.md Section 13.
    """
