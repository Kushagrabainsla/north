"""Tool ABC hierarchy. See README Section 7 and docs/CODING_STYLE.md Section 16.1."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from tools.models import ToolInput, ToolOutput

if TYPE_CHECKING:
    from approval.judgement_filter import JudgementFilter
    from approval.store import ApprovalStore
    from orchestrator.stream import EventStreamManager


class Tool(ABC):
    """Base class for every tool an agent can call.

    Subclasses set the class-level `name` and `description` strings and
    implement `run()`. The Orchestrator only ever sees this interface  - 
    it does not know whether the concrete tool hits an external API, a
    local cache, or a mocked test double.
    """

    name: str
    description: str
    # Whether running this tool mutates the filesystem or external state. The
    # agent loop runs read-only tools concurrently but serializes mutating ones
    # so two edits to the same file can't race (lost update). Default False;
    # mutating tools opt in (OCP - no central switch-on-name).
    is_mutating: bool = False
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
        recoverable errors - return `ToolOutput(success=False, error=...)`
        instead so the `ConfidenceTracker` can record the outcome."""

    def format_output(self, data: dict[str, Any]) -> str:
        """Render a successful ToolOutput.data as a human-readable string.

        The default falls back to compact JSON. Subclasses override to produce
        domain-appropriate text (e.g. WriteFileTool returns a one-liner with
        the path and byte count). The Orchestrator calls this instead of
        maintaining a central switch-on-tool-name.
        """
        return json.dumps(data, indent=2) if data else "Done."


class ApprovalGatedTool(Tool, ABC):
    """Base class for tools that gate actions behind user approval."""

    def __init__(
        self,
        approval_store: ApprovalStore | None = None,
        stream_manager: EventStreamManager | None = None,
        approval_timeout_seconds: float = 300.0,
        judgement_filter: JudgementFilter | None = None,
    ) -> None:
        self._approval_store = approval_store
        self._stream_manager = stream_manager
        self._approval_timeout_seconds = approval_timeout_seconds
        self._judgement_filter = judgement_filter


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
