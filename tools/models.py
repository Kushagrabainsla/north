"""Models for the tool layer. See README Section 7."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


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
    human-readable message on failure. The Orchestrator uses `success`
    to update tool confidence (see `ConfidenceTracker`).
    """

    success: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class ConfidenceScore(BaseModel):
    """One persisted (agent, tool) confidence pair. Schema mirrors README 7.5."""

    agent: str
    tool: str
    confidence: float
    uses_total: int
    uses_helpful: int
    last_updated: datetime
