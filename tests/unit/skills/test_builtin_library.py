"""Integrity + quality checks for the shipped built-in skill library.

Guards the actual built-in skills under resources/builtin-skills/ so a malformed, generic, or
overlapping skill cannot ship: every skill must have a trigger-oriented
description and a procedural body, descriptions must be distinct (top-2 semantic
selection collides otherwise), and the deliberate cut/merge decisions stay made.
"""

from __future__ import annotations

from skills.registry import SkillRegistry
from utils.runtime_resources import builtin_skills_dir

BUILTIN_DIR = builtin_skills_dir()
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
        "alignment-and-grilling",
        "deep-module-architecture",
        "tracer-bullet-ticket-decomposition",
        "interactive-human-wizard",
        "two-axis-code-review",
        "plain-english-reframing",
        "adding-a-north-tool",
        "authoring-a-north-flow",
        "authoring-a-north-skill",
        "authoring-a-north-agent",
        "scheduling-north-work",
        "preparing-job-applications",
        "submitting-an-approved-job-application",
        "processing-a-job-application-queue",
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


def test_browser_and_self_extension_guidance_reaches_the_general_agent():
    for name in (
        "browser-research-and-extraction",
        "adding-a-north-tool",
        "authoring-a-north-flow",
        "authoring-a-north-skill",
        "authoring-a-north-agent",
        "scheduling-north-work",
    ):
        assert _REGISTRY.get(name).available_to("general"), name


def test_browser_skill_has_an_enforced_flow_contract():
    skill = _REGISTRY.get("browser-research-and-extraction")

    assert skill.execution is not None
    assert skill.execution.agent == "general"
    assert skill.execution.tools == ("browser",)
    assert skill.execution.approval == "on_mutation"
    assert "profile_id" not in skill.execution.inputs.get("properties", {})
    assert "browser_context" not in skill.execution.inputs.get("required", [])


def test_flow_authoring_requires_complete_setup_and_explicit_timing() -> None:
    body = _REGISTRY.get("authoring-a-north-flow").body
    assert "before calling `create_flow:create`" in body
    for choice in ("Outcome and evidence", "Inputs and preferences", "Outputs and delivery", "Scope and safety"):
        assert choice in body
    for timing in ("Manual only", "Once at a specific time", "Recurring"):
        assert timing in body
    assert 'call `ask_user`: "When should this flow run?"' in body
    assert "Do not silently default to manual" in body
    assert "date and local time" in body
    assert "days and local time" in body
    assert "timezone" in body
    assert "timezone is not a flow input or question" in body
    assert "Do not ask for timezone" in body
    assert "configured in North settings" in body
    assert "do not ask again for details already supplied" in body
    assert "preserve existing timing" in body
    assert "pending, not installed" in body
    assert "Choosing timing is not approval to test, activate, or install it" in body


def test_scheduling_skill_clarifies_incomplete_timing() -> None:
    body = _REGISTRY.get("scheduling-north-work").body
    assert "`ask_user` for missing timing details" in body
    assert 'Never invent a time from "daily"' in body
    assert "Do not ask again for timing already supplied" in body
    assert "Do not ask for timezone or pass an override" in body


def test_job_application_skills_split_drafting_from_submission() -> None:
    prepare = _REGISTRY.get("preparing-job-applications")
    submit = _REGISTRY.get("submitting-an-approved-job-application")

    assert prepare.execution is not None
    assert prepare.execution.approval == "before_step"
    assert {"browser", "read_file", "write_file"} == set(prepare.execution.tools)
    assert "never submits" in prepare.body.lower()

    assert submit.execution is not None
    assert submit.execution.approval == "before_step"
    assert submit.execution.tools == ("browser", "write_file")
    assert "exactly once" in submit.body.lower()

    queue = _REGISTRY.get("processing-a-job-application-queue")
    assert queue.execution is not None
    assert queue.execution.approval == "before_step"
    assert "request_approval" in queue.body
    assert "returned `response` values" in queue.body
