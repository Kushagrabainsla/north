"""Ledger-specific exceptions. See docs/CODING_STYLE.md Section 13.1."""

from __future__ import annotations

from exceptions import NorthError


class LedgerError(NorthError):
    """Base class for ledger-related failures."""


class LedgerWriteError(LedgerError):
    """Raised when a ledger write fails."""


class LedgerReadError(LedgerError):
    """Raised when a ledger read or query fails."""
