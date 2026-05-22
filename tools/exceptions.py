"""Tool-layer exceptions."""

from __future__ import annotations

from exceptions import NorthError


class ToolError(NorthError):
    """Base class for tool-layer failures."""


class ToolNotFoundError(ToolError):
    """Raised when a tool name is not registered."""


class ToolExecutionError(ToolError):
    """Raised when a tool's `run()` raises or returns failure."""


class ToolAuthError(ToolError):
    """Raised when an `AuthenticatedTool`'s credentials are invalid."""
