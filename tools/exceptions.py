"""Tool-layer exceptions."""

from __future__ import annotations

from exceptions import NorthError
from utils.tools import ToolNotFoundError as ToolNotFoundContract


class ToolError(NorthError):
    """Base class for tool-layer failures."""


class ToolNotFoundError(ToolError, ToolNotFoundContract):
    """Raised when a tool name is not registered.

    Also subclasses the platform ``utils.tools.ToolNotFoundError`` contract so the
    orchestrator - which catches the platform type without importing this module -
    handles a missing tool through the port. Multiple inheritance keeps the
    existing ``ToolError``/``NorthError`` behaviour for lower-layer callers.
    """


class ToolExecutionError(ToolError):
    """Raised when a tool's `run()` raises or returns failure."""


class ToolAuthError(ToolError):
    """Raised when an `AuthenticatedTool`'s credentials are invalid."""
