"""Agent-layer exceptions."""

from __future__ import annotations

from exceptions import NorthError


class AgentError(NorthError):
    """Base class for agent-layer failures."""


class AgentNotFoundError(AgentError):
    """Raised when an agent name is not registered."""


class AgentExecutionError(AgentError):
    """Raised when an agent's `_execute()` fails irrecoverably."""


class AgentConfigError(AgentError):
    """Raised when `config.yaml` is missing, malformed, or contradicts the runtime."""


class AgentOutputParseError(AgentError):
    """Raised when an LLM-backed agent returns output that can't be parsed."""
