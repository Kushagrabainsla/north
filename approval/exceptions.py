"""Custom exceptions for the Approval Layer.

See docs/CODING_STYLE.md Section 13.
"""

from __future__ import annotations

from exceptions import NorthError


class ApprovalError(NorthError):
    """Base class for all approval and notification errors."""
