"""Pydantic schemas for bootstrap fact extraction with structured output validation."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field


class FactSubject(StrEnum):
    """Subject classification for extracted facts."""

    USER = "user"
    THIRD_PARTY = "third_party"
    ORGANIZATION = "organization"
    UNKNOWN = "unknown"


class FactTopic(StrEnum):
    """What area of the user's life a fact belongs to.

    Deliberately the same set as ``UserProfile``'s sections plus two the profile
    has no place for: an identity fact (name, status, who the user is) and a
    catch-all. This is what lets a caller ask for the *kind* of fact a task needs
    - a coding agent has real use for preferences and skills and none at all for
    tax figures - which a single flat category cannot express.
    """

    IDENTITY = "identity"
    EDUCATION = "education"
    JOBS = "jobs"
    SKILLS = "skills"
    FINANCES = "finances"
    HEALTH = "health"
    SCHEDULE = "schedule"
    PREFERENCES = "preferences"
    PROJECTS = "projects"
    OTHER = "other"


class FactStatus(StrEnum):
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
    topic: FactTopic = Field(
        default=FactTopic.OTHER,
        description=(
            "Which area of life this fact belongs to. Use 'identity' for who the user is "
            "(name, nationality, student/visa status), and 'other' only when nothing else fits."
        ),
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
    topic: FactTopic
    confidence: float
    source_path: str
    source_hash: str
    source_mtime: float
    evidence: str | None = None


class UserProfile(BaseModel):
    """Structured user profile extracted from a single file.

    Each section is a list of short, specific facts. Empty sections are fine —
    a resume yields education/jobs/skills; a meal plan yields health/preferences.
    """

    education: list[str] = Field(default_factory=list, description="Schools, degrees, courses, enrollment.")
    jobs: list[str] = Field(default_factory=list, description="Roles, employers, internships, job searches.")
    skills: list[str] = Field(default_factory=list, description="Technical and soft skills, languages, tools.")
    finances: list[str] = Field(default_factory=list, description="Budget, income, expenses, savings, subscriptions.")
    health: list[str] = Field(default_factory=list, description="Diet, exercise, sleep, medical, meals.")
    schedule: list[str] = Field(default_factory=list, description="Recurring meetings, deadlines, routines, timezone.")
    preferences: list[str] = Field(default_factory=list, description="Likes, dislikes, communication style, defaults.")
    projects: list[str] = Field(default_factory=list, description="Active projects, repos, hackathons, coursework.")


class UnifiedBootstrapExtraction(BaseModel):
    """Unified structured output containing both atomic facts and structured profile domains."""

    facts: list[FactItem] = Field(
        default_factory=list,
        max_length=15,
        description="List of atomic personal facts extracted from the document.",
    )
    profile: UserProfile = Field(
        default_factory=UserProfile,
        description="Structured domain profile extracted from the document.",
    )


def _as_strict(schema: dict, name: str) -> dict:
    """Make a Pydantic JSON Schema satisfy strict structured-output mode.

    Declaring ``strict`` is a promise about the *schema*, not just a flag: every
    object must set ``additionalProperties: false`` and list every property in
    ``required``. Pydantic emits neither - it omits ``additionalProperties`` and
    leaves defaulted fields out of ``required`` - so a schema marked strict here
    was rejected outright by the one provider that actually enforces the contract
    ("'additionalProperties' is required to be supplied and to be false"), while
    lenient providers ignored the flag entirely. It has therefore never been
    enforced anywhere; this makes the promise true.

    Requiring a defaulted field is safe for these models: it asks the model to
    always emit the key (``evidence`` is already nullable, and an empty profile
    section is an empty list, which the model is told is fine). Python-side
    defaults are unaffected - callers still read the parsed result with ``.get``.
    """
    strict = deepcopy(schema)
    for node in _object_nodes(strict):
        node["additionalProperties"] = False
        node["required"] = list(node.get("properties", {}))
    _drop_ref_siblings(strict)
    strict["name"] = name
    strict["strict"] = True
    return strict


# Keywords Pydantic writes next to a "$ref" (for a field that points at an enum or
# a nested model and also carries a default or a description). Strict mode rejects
# a "$ref" with any sibling at all; the referenced definition keeps its own
# description, and a default is meaningless once every property is required.
_REF_SIBLINGS = ("default", "description", "title")


def _drop_ref_siblings(node: object) -> None:
    """Strip the keywords strict mode forbids alongside a ``$ref``."""
    if isinstance(node, dict):
        if "$ref" in node:
            for keyword in _REF_SIBLINGS:
                node.pop(keyword, None)
        for value in node.values():
            _drop_ref_siblings(value)
    elif isinstance(node, list):
        for value in node:
            _drop_ref_siblings(value)


def _object_nodes(node: object):
    """Every object-typed subschema, including those under ``$defs``."""
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            yield node
        for value in node.values():
            yield from _object_nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _object_nodes(value)


# JSON Schema for OpenAI-compatible structured output (response_format)
EXTRACTED_FACTS_JSON_SCHEMA = _as_strict(ExtractedFacts.model_json_schema(), "extracted_facts")
USER_PROFILE_JSON_SCHEMA = _as_strict(UserProfile.model_json_schema(), "user_profile")
UNIFIED_EXTRACTION_JSON_SCHEMA = _as_strict(
    UnifiedBootstrapExtraction.model_json_schema(), "unified_bootstrap_extraction"
)