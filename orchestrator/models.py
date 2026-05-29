"""Models and data structures for the Orchestrator.

See docs/CODING_STYLE.md Section 9.7.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from ledger.models import LedgerSource


class ExecutionMode(str, Enum):
    """Execution structure chosen by the router for a given task."""
    SINGLE_TOOL = "single_tool"      # one deterministic tool call, no agent
    SINGLE_AGENT = "single_agent"    # one agent's ReAct loop
    PARALLEL = "parallel"            # independent agents fan out simultaneously
    HIERARCHICAL = "hierarchical"    # agents run in dependency order


class TaskRequest(BaseModel):
    """Input payload to trigger a new task execution."""

    prompt: str = Field(..., min_length=1, max_length=32_768)
    source: LedgerSource = LedgerSource.PROMPT
    workspace: str = ""  # optional root directory for filesystem/shell tools


class TaskResponse(BaseModel):
    """Response returned upon successfully registering a task."""

    task_id: str
    status: str
    created_at: str


class IntentClassification(BaseModel):
    """Result of intent classification."""

    is_consequential: bool
    domain: str
    reasoning: str
    confidence: float = 1.0  # 0–1; below 0.7 skips the north star check to avoid false interruptions


class ExecutionPlan(BaseModel):
    """The plan built by the router indicating execution steps."""

    task_id: str
    agents: list[str]
    parallel_groups: list[list[str]]
    dependencies: dict[str, list[str]]
    mode: ExecutionMode = ExecutionMode.SINGLE_AGENT
    direct_tool: str | None = None
    direct_tool_params: dict[str, Any] = Field(default_factory=dict)
