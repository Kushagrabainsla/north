"""Custom exceptions for the Orchestrator layer.

See docs/CODING_STYLE.md Section 13.
"""

from __future__ import annotations

from exceptions import NorthError


class OrchestratorError(NorthError):
    """Base exception for all orchestrator failures."""


class NorthStarConflictError(OrchestratorError):
    """Raised when a task conflicts with the user's North Star goals."""


class RoutingError(OrchestratorError):
    """Raised when intent routing or execution plan construction fails."""


class ClassifierError(OrchestratorError):
    """Raised when intent classification fails."""
