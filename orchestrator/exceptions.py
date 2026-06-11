"""Custom exceptions for the Orchestrator layer.

See docs/CODING_STYLE.md Section 13.
"""

from __future__ import annotations

from exceptions import NorthError


class OrchestratorError(NorthError):
    """Base exception for all orchestrator failures."""


class NorthStarConflictError(OrchestratorError):
    """Raised when a task conflicts with the user's North Star goals."""


class TaskCapacityError(OrchestratorError):
    """Raised when the concurrent-task cap is reached. API routes map this to HTTP 429."""


class RoutingError(OrchestratorError):
    """Raised when intent routing or execution plan construction fails."""
