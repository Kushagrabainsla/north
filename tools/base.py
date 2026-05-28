"""Tool ABC hierarchy. See README Section 7 and docs/CODING_STYLE.md Section 16.1."""

from __future__ import annotations

from abc import ABC, abstractmethod

from tools.models import ToolInput, ToolOutput


class Tool(ABC):
    """Base class for every tool an agent can call.

    Subclasses set the class-level `name` and `description` strings and
    implement `run()`. The Orchestrator only ever sees this interface —
    it does not know whether the concrete tool hits an external API, a
    local cache, or a mocked test double.
    """

    name: str
    description: str
    # Override in subclasses with an OpenAI-compatible JSON Schema for the
    # function parameters.  The default accepts any key/value object.
    parameters_schema: dict = {
        "type": "object",
        "additionalProperties": True,
    }

    def schema(self) -> dict:
        """Return an OpenAI-format function definition for use with tool calling."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }

    @abstractmethod
    async def run(self, input: ToolInput) -> ToolOutput:
        """Execute the tool against `input.params`. Must not raise on
        recoverable errors — return `ToolOutput(success=False, error=...)`
        instead so the `ConfidenceTracker` can record the outcome."""


class AuthenticatedTool(Tool, ABC):
    """A tool that requires verifying credentials before use."""

    @abstractmethod
    async def validate_credentials(self) -> bool:
        """Return True if stored credentials are valid for this tool."""


class CacheableTool(Tool, ABC):
    """A tool whose results can be cached by key."""

    @abstractmethod
    async def get_cached(self, key: str) -> ToolOutput | None:
        """Return a previously cached result for `key`, or None on miss."""

    @abstractmethod
    async def set_cached(self, key: str, result: ToolOutput) -> None:
        """Store `result` under `key`."""
