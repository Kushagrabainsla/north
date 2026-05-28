"""Pydantic models and enums for the Ledger. See README Section 4."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class LedgerSource(str, Enum):
    """Origin of a Ledger entry. Canonical list — see README Section 4.3."""

    PROMPT = "prompt"
    MIC = "mic"
    CRON = "cron"
    AGENT = "agent"
    ASYNC = "async"
    SYSTEM = "system"
    MANUAL_INJECTION = "manual_injection"
    INFERENCE_ROUTER = "inference_router"
    APPROVAL = "approval"
    WEBHOOK = "webhook"


class LedgerStatus(str, Enum):
    """Lifecycle status of a Ledger entry. See README Section 4.2 schema."""

    COMPLETED = "completed"
    PENDING = "pending"
    FAILED = "failed"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    AWAITING_INPUT = "awaiting_input"


class LedgerEntry(BaseModel):
    """One immutable record in the append-only audit trail.

    Schema mirrors README Section 4.2. Two output fields are intentional:
    `output` is a human-readable summary for the UI; `agent_output` is the
    full structured JSON used to reconstruct Task Context on failure recovery.
    """

    id: str
    timestamp: datetime
    source: LedgerSource

    task_id: str | None = None
    agent: str | None = None
    input: str | None = None
    action: str | None = None
    output: str | None = None
    agent_output: dict[str, Any] | None = None
    tools_used: list[str] = Field(default_factory=list)
    model_used: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    status: LedgerStatus | None = None
