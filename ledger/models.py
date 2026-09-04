"""Pydantic models and enums for the Ledger. See README Section 4."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from utils.execution_context import current_execution
from utils.ids import generate_id
from utils.time import utcnow


class LedgerSource(StrEnum):
    """Origin of a Ledger entry. Canonical list - see README Section 4.3."""

    PROMPT = "prompt"
    MIC = "mic"
    CRON = "cron"
    AGENT = "agent"
    ASYNC = "async"
    SYSTEM = "system"
    MANUAL_INJECTION = "manual_injection"
    INFERENCE_ROUTER = "inference_router"
    APPROVAL = "approval"
    CLARIFICATION = "clarification"  # the user's answer to an ask_user question - learnable
    WEBHOOK = "webhook"


class LedgerStatus(StrEnum):
    """Lifecycle status of a Ledger entry. See README Section 4.2 schema."""

    COMPLETED = "completed"
    PENDING = "pending"
    FAILED = "failed"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    AWAITING_INPUT = "awaiting_input"


class LedgerSummary(BaseModel):
    """The narrow projection of a ledger row: everything except the bulk text.

    Callers that scan a task's history to answer a structural question - what
    status did it reach, which model did an agent use, which tools ran - need
    none of `input`, `output`, or `agent_output`, and those are by far the
    largest columns (a full agent answer each). Reading them back and building
    a `LedgerEntry` per row costs far more than the question is worth, so those
    call sites use `LedgerWriter.query_summaries()` and get this instead.
    """

    id: str
    timestamp: datetime
    source: LedgerSource
    task_id: str | None = None
    run_id: str | None = None
    agent: str | None = None
    action: str | None = None
    tools_used: list[str] = Field(default_factory=list)
    model_used: str | None = None
    status: LedgerStatus | None = None
    error_type: str | None = None


class LedgerEntry(BaseModel):
    """One immutable record in the append-only audit trail.

    Schema mirrors README Section 4.2. Two output fields are intentional:
    `output` is a human-readable summary for the UI; `agent_output` is the
    full structured JSON used to reconstruct Task Context on failure recovery.
    """

    id: str
    timestamp: datetime
    source: LedgerSource

    @classmethod
    def new(cls, source: LedgerSource, **fields: Any) -> LedgerEntry:
        """Create an entry with a generated id and the current UTC timestamp.

        Every writer needs exactly this trio; the factory keeps call sites to
        the fields that actually vary (CODING_STYLE §5 DRY).
        """
        execution = current_execution()
        if execution is not None:
            fields.setdefault("run_id", execution.run_id)
            fields.setdefault("parent_run_id", execution.parent_run_id)
            fields.setdefault("attempt", execution.attempt)
        return cls(id=generate_id(), timestamp=utcnow(), source=source, **fields)

    task_id: str | None = None
    run_id: str | None = None
    parent_run_id: str | None = None
    attempt: int | None = None
    agent: str | None = None
    input: str | None = None
    action: str | None = None
    output: str | None = None
    agent_output: dict[str, Any] | None = None
    tools_used: list[str] = Field(default_factory=list)
    model_used: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    # Prompt tokens the provider served from its cache (see inference/usage.py).
    # None when the provider said nothing about caching, which is not the same as
    # a cache miss.
    cached_tokens: int | None = None
    cost_usd: float | None = None
    status: LedgerStatus | None = None
    duration_ms: int | None = None
    error_type: str | None = None
