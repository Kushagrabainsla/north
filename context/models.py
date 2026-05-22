"""Models and enums for the Context Layer. See README Section 5."""

from __future__ import annotations

from enum import Enum


class ContextDocument(str, Enum):
    """The five markdown files that constitute north's context. See README 5.1.

    Members are the file names on disk. Using the enum at API boundaries means
    no caller can ask for an unknown document — the type system rejects it.
    """

    PUBLIC = "public.md"
    PRIVATE = "private.md"
    PRIVACY_RULES = "privacy_rules.md"
    JUDGEMENT_RULES = "judgement_rules.md"
    NORTH_STARS = "north_stars.md"
