"""Pydantic schemas for bootstrap fact extraction with structured output validation."""

from __future__ import annotations

from pydantic import BaseModel, Field


class FactItem(BaseModel):
    """A single extracted fact about the user."""

    content: str = Field(
        ...,
        min_length=10,
        max_length=500,
        description="Specific factual statement about the user (habits, finances, health, schedule, preferences, background).",
    )


class ExtractedFacts(BaseModel):
    """Structured output for bootstrap fact extraction."""

    facts: list[FactItem] = Field(
        default_factory=list,
        max_length=10,
        description="List of extracted facts (max 5 per file, but schema allows up to 10 for safety).",
    )


# JSON Schema for OpenAI-compatible structured output (response_format)
EXTRACTED_FACTS_JSON_SCHEMA = ExtractedFacts.model_json_schema()
EXTRACTED_FACTS_JSON_SCHEMA["name"] = "extracted_facts"
EXTRACTED_FACTS_JSON_SCHEMA["strict"] = True