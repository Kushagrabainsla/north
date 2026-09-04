"""Models for the tool layer. See README Section 7."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class ToolInput(BaseModel):
    """Envelope for parameters passed into a tool's `run()` method.

    Tools accept a structured input rather than positional args so the
    `Tool` ABC stays uniform across every concrete implementation.
    """

    params: dict[str, Any] = Field(default_factory=dict)


class ToolOutput(BaseModel):
    """Result of running a tool.

    `success` is the single source of truth for whether the call worked.
    `data` carries the structured result on success; `error` carries a
    human-readable message on failure.

    `failure_kind` says *why* it did not work, because three different things
    were being flattened into one `success: false` and read as the same thing:

    - ``"error"``    - the tool broke. The default, and the only kind that
      counts against the tool in `ConfidenceTracker`.
    - ``"not_found"`` - the tool worked and the answer is "that isn't there".
      Asking whether a file exists is not a malfunction. Left unmarked, an
      agent told to stop on failure aborted the whole task over an absent
      optional file, and `read_file` decayed to the lowest-ranked tool the
      researcher had, purely from being asked a question it answered
      correctly.
    - ``"refused"``  - a human (or a timed-out approval) declined the action.
      Nothing is wrong with the tool.

    Only `error` should be treated as a fault by anything downstream.
    """

    success: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    failure_kind: Literal["error", "not_found", "refused"] | None = None

    @model_validator(mode="after")
    def _default_failure_kind(self) -> ToolOutput:
        """A failure with no stated kind is an error - the safe assumption."""
        if not self.success and self.failure_kind is None:
            object.__setattr__(self, "failure_kind", "error")
        return self


class ConfidenceScore(BaseModel):
    """One persisted (agent, tool) confidence pair. Schema mirrors README 7.5."""

    agent: str
    tool: str
    confidence: float
    uses_total: int
    uses_helpful: int
    last_updated: datetime
