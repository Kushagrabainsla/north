"""Tests for context models and enums (README Section 5.1)."""

from __future__ import annotations

from context import ContextDocument


def test_context_document_enum_matches_spec() -> None:
    """The five document file names must match README Section 5.1 verbatim."""
    expected = {
        "public.md",
        "private.md",
        "privacy_rules.md",
        "judgement_rules.md",
        "north_stars.md",
    }
    assert {d.value for d in ContextDocument} == expected


def test_context_document_count_is_exactly_five() -> None:
    """README Section 5 is explicit that there are five documents and only five."""
    assert len(list(ContextDocument)) == 5
