"""Tests for context models and enums (README Section 5.1)."""

from __future__ import annotations

from memory import ContextDocument


def test_context_document_enum_matches_spec() -> None:
    """The document file names must match the memory layer spec verbatim."""
    expected = {
        "user.md",
        "judgement_rules.md",
        "north_stars.md",
        "soul.md",
    }
    assert {d.value for d in ContextDocument} == expected


def test_context_document_count_is_exactly_four() -> None:
    """user, judgement_rules, north_stars, and soul - four documents, no more."""
    assert len(list(ContextDocument)) == 4
