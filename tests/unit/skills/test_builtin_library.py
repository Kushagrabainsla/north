"""Integrity + quality checks for the shipped built-in skill library.

Guards the actual skills under skills/builtin/ so a malformed, generic, or
overlapping skill cannot ship: every skill must have a trigger-oriented
description and a procedural body, descriptions must be distinct (top-2 semantic
selection collides otherwise), and the deliberate cut/merge decisions stay made.
"""

from __future__ import annotations

from pathlib import Path

from skills.registry import SkillRegistry

BUILTIN_DIR = Path(__file__).resolve().parents[3] / "skills" / "builtin"
_REGISTRY = SkillRegistry(builtin_dir=BUILTIN_DIR)
_SKILLS = _REGISTRY.all()

# OpenCode/Anthropic spec cap on the description field.
_MAX_DESCRIPTION_CHARS = 1024


def test_library_has_the_full_set():
    assert len(_SKILLS) >= 25


def test_every_skill_is_well_formed():
    for skill in _SKILLS:
        assert skill.description.startswith("Use "), f"{skill.name}: description must be a trigger ('Use when ...')"
        assert len(skill.description) <= _MAX_DESCRIPTION_CHARS, f"{skill.name}: description too long"
        assert skill.body.strip(), f"{skill.name}: empty body"
        # Built-in skills are curated and may exceed the learned-skill length cap
        # (the cap guards auto-distilled skills, not hand-authored ones), so no
        # MAX_BODY_CHARS assertion here.
        assert "1." in skill.body, f"{skill.name}: body must contain a numbered procedure"


def test_descriptions_are_distinct():
    # Identical descriptions would make top-2 semantic selection collide.
    descriptions = [s.description.lower() for s in _SKILLS]
    assert len(descriptions) == len(set(descriptions))


def test_key_skills_present():
    names = set(_REGISTRY.names())
    expected = {
        "systematic-debugging",
        "test-design-and-regression-coverage",  # merged: edge-cases + TDD + effective-tests
        "error-handling-and-failure-modes",
        "scouting-open-source-contributions",  # user-requested
        "safe-refactoring",
        "security-and-hardening",
        "conducting-a-literature-review",  # general-domain research/synthesis skill
    }
    assert expected <= names


def test_cut_skills_absent():
    # These were cut as pure prompt-duplication; they must not reappear as skills.
    names = set(_REGISTRY.names())
    assert "minimal-surgical-change" not in names
    # Note: incremental-implementation was previously cut but is now re-added as a
    # substantially richer skill (slicing strategies, implementation rules 0-5,
    # rollback-friendly patterns) that goes well beyond the coder prompt.


def test_research_skill_serves_general_not_engineering():
    # The literature-review skill routes to the general assistant; it must never leak
    # into an engineering agent's context (which uses the code-first researcher instead).
    skill = _REGISTRY.get("conducting-a-literature-review")
    assert skill.available_to("general")
    assert not skill.available_to("engineering")
