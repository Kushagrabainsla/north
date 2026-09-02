"""Models and data structures for the Orchestrator.

See docs/CODING_STYLE.md Section 9.7.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from ledger.models import LedgerSource


class ExecutionPath(StrEnum):
    """Routing path tier: FAST for simple/single-stage tasks; DEEP for multi-stage pipelines."""

    FAST = "fast"
    DEEP = "deep"


class ExecutionMode(StrEnum):
    """Execution structure chosen by the router for a given task."""

    SINGLE_TOOL = "single_tool"  # one deterministic tool call, no agent
    SINGLE_AGENT = "single_agent"  # one agent's ReAct loop
    PARALLEL = "parallel"  # independent agents fan out simultaneously
    HIERARCHICAL = "hierarchical"  # agents run in dependency order


class TaskRequest(BaseModel):
    """Input payload to trigger a new task execution."""

    prompt: str = Field(..., min_length=1, max_length=32_768)
    source: LedgerSource = LedgerSource.PROMPT
    workspace: str = ""  # optional root directory for filesystem/shell tools
    context: str = ""  # optional pre-loaded context summary to inject into the agent's prompt
    # Optional client-supplied key to dedupe re-deliveries (e.g. a webhook id).
    # When omitted, the orchestrator derives one from source+prompt.
    idempotency_key: str | None = None
    # When set, run this specific agent directly, bypassing intent classification
    # and the planner. Backs `north agent run <name>` so a manual agent trigger
    # invokes exactly that agent instead of being re-routed by the planner.
    forced_agent: str | None = None


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
    execution_path: ExecutionPath = ExecutionPath.FAST
    # No north-star fields here on purpose. The planner prompt is never given the
    # user's goals, so anything it said about alignment was a guess with no input;
    # those fields sat on this model unread, waiting for someone to trust them.
    # NorthStarChecker reads north_stars.md and makes that judgement (Stage 2).


class ExecutionPlan(BaseModel):
    """The plan built by the router indicating execution steps."""

    task_id: str
    agents: list[str]
    parallel_groups: list[list[str]]
    dependencies: dict[str, list[str]]
    mode: ExecutionMode = ExecutionMode.SINGLE_AGENT
    execution_path: ExecutionPath = ExecutionPath.FAST
    direct_tool: str | None = None
    direct_tool_params: dict[str, Any] = Field(default_factory=dict)
    # For engineering tasks: the classified kind (question/research/bugfix/refactor/
    # feature). Empty for non-engineering plans. Lets the DoD gate apply kind-specific
    # evidence checks (e.g. a bugfix should carry reproduction + regression evidence).
    engineering_kind: str = ""

    def with_task_id(self, new_task_id: str) -> ExecutionPlan:
        """Return a copy of this plan with task_id replaced."""
        return self.model_copy(update={"task_id": new_task_id})
