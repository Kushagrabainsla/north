"""Pydantic schemas for bootstrap fact extraction with structured output validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field


class FactSubject(str, Enum):
    """Subject classification for extracted facts."""

    USER = "user"
    THIRD_PARTY = "third_party"
    ORGANIZATION = "organization"
    UNKNOWN = "unknown"


class FactStatus(str, Enum):
    """Status of a stored fact."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    UNCERTAIN = "uncertain"


class FactItem(BaseModel):
    """A single extracted fact about the user."""

    content: str = Field(
        ...,
        min_length=10,
        max_length=500,
        description=(
            "Specific factual statement about the user "
            "(habits, finances, health, schedule, preferences, background)."
        ),
    )
    subject: FactSubject = Field(
        default=FactSubject.USER,
        description="Who or what this fact is about.",
    )
    confidence: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="Confidence that this fact is accurate and about the subject.",
    )
    evidence: str | None = Field(
        default=None,
        max_length=500,
        description="Short quote from the source supporting this fact.",
    )


class ExtractedFacts(BaseModel):
    """Structured output for bootstrap fact extraction."""

    facts: list[FactItem] = Field(
        default_factory=list,
        max_length=10,
        description="List of extracted facts (max 5 per file, but schema allows up to 10 for safety).",
    )


@dataclass
class FactCandidate:
    """A candidate fact before validation and persistence."""

    content: str
    subject: FactSubject
    confidence: float
    source_path: str
    source_hash: str
    source_mtime: float
    evidence: str | None = None


# JSON Schema for OpenAI-compatible structured output (response_format)
EXTRACTED_FACTS_JSON_SCHEMA = ExtractedFacts.model_json_schema()
EXTRACTED_FACTS_JSON_SCHEMA["name"] = "extracted_facts"
EXTRACTED_FACTS_JSON_SCHEMA["strict"] = True