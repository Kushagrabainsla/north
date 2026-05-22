"""Models and data structures for the Orchestrator.

See docs/CODING_STYLE.md Section 9.7.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from ledger.models import LedgerSource


class TaskRequest(BaseModel):
    """Input payload to trigger a new task execution."""

    prompt: str
    source: LedgerSource = LedgerSource.PROMPT


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


@dataclass
class ExecutionPlan:
    """The plan built by the router indicating execution steps."""

    task_id: str
    agents: list[str]
    parallel_groups: list[list[str]]
    dependencies: dict[str, list[str]]
