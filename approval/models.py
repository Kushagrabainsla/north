"""Pydantic models and enums for the Approval Layer.

See docs/CODING_STYLE.md Section 7.3 and README Section 9.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class CardType(str, Enum):
    """The three supported card types in north."""

    INFORMATION = "information"
    APPROVAL = "approval"
    QUESTION = "question"


class ApprovalDecision(str, Enum):
    """Possible outcomes for a user approval card decision."""

    APPROVED = "approved"
    REJECTED = "rejected"


class Card(BaseModel):
    """A card presented to the user via macOS notification or Web UI."""

    id: str
    type: CardType
    task_id: str
    agent: str
    title: str
    message: str
    options: list[str] = Field(default_factory=list)
    status: str = "pending"  # pending, approved, rejected, answered
    chosen_option: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
