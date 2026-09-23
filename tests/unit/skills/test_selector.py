"""Tests for SkillSelector semantic selection, thresholding, and fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from skills.registry import SkillRegistry
from skills.selector import SkillSelector

_VOCAB = ("migration", "tool", "test")


async def _fake_embed(texts: list[str]) -> list[list[float]]:
    """A deterministic bag-of-keywords embedding over a tiny fixed vocabulary."""
    vectors: list[list[float]] = []
    for text in texts:
        lowered = text.lower()
        vector = [1.0 if word in lowered else 0.0 for word in _VOCAB]
        if not any(vector):
            vector = [0.0, 0.0, 0.001]  # non-zero so cosine is defined
        vectors.append(vector)
    return vectors


def _write_skill(base: Path, name: str, description: str, intents: list[str] | None = None) -> None:
    directory = base / name
    directory.mkdir(parents=True)
    intent_line = f"intents: {intents}\n" if intents else ""
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{intent_line}---\nbody for {name}", encoding="utf-8"
    )


def _registry(base: Path) -> SkillRegistry:
    _write_skill(base, "db-migration", "Use when adding a database migration")
    _write_skill(base, "add-tool", "Use when adding a tool")
    return SkillRegistry(builtin_dir=base)


async def test_selects_the_matching_skill(tmp_path):
    selector = SkillSelector(_registry(tmp_path), embed_fn=_fake_embed, min_similarity=0.5)
    picked = await selector.select("how do I add a database migration?")
    assert [s.name for s in picked] == ["db-migration"]


async def test_below_threshold_returns_nothing(tmp_path):
    selector = SkillSelector(_registry(tmp_path), embed_fn=_fake_embed, min_similarity=0.5)
    assert await selector.select("something totally unrelated banana") == []


async def test_no_embed_fn_returns_nothing(tmp_path):
    selector = SkillSelector(_registry(tmp_path), embed_fn=None)
    assert await selector.select("add a migration") == []


async def test_empty_task_returns_nothing(tmp_path):
    selector = SkillSelector(_registry(tmp_path), embed_fn=_fake_embed)
    assert await selector.select("   ") == []


async def test_respects_top_k(tmp_path):
    selector = SkillSelector(_registry(tmp_path), embed_fn=_fake_embed, top_k=1, min_similarity=0.4)
    picked = await selector.select("add a migration tool")  # matches both skills
    assert len(picked) == 1


async def test_adds_a_distinct_secondary_capability(tmp_path):
    selector = SkillSelector(_registry(tmp_path), embed_fn=_fake_embed, min_similarity=0.4)

    picked = await selector.select("add a migration tool")

    assert {s.name for s in picked} == {"db-migration", "add-tool"}


async def test_skips_a_secondary_that_duplicates_the_primary_capability(tmp_path):
    _write_skill(tmp_path, "migration-plan", "Use when planning a database migration")
    _write_skill(tmp_path, "migration-runbook", "Use when executing a database migration")
    selector = SkillSelector(SkillRegistry(builtin_dir=tmp_path), embed_fn=_fake_embed, min_similarity=0.5)

    picked = await selector.select("plan a database migration")

    assert len(picked) == 1


async def test_reembeds_a_skill_when_its_description_changes(tmp_path):
    _write_skill(tmp_path, "changing-skill", "Use for a migration")
    registry = SkillRegistry(builtin_dir=tmp_path)
    selector = SkillSelector(registry, embed_fn=_fake_embed, min_similarity=0.5)

    assert [skill.name for skill in await selector.select("migration")] == ["changing-skill"]

    skill_file = tmp_path / "changing-skill" / "SKILL.md"
    skill_file.write_text(
        "---\nname: changing-skill\ndescription: Use for a tool\n---\nbody",
        encoding="utf-8",
    )
    registry.reload()

    assert await selector.select("migration") == []
    assert [skill.name for skill in await selector.select("tool")] == ["changing-skill"]


async def test_intent_filter_rejects_semantically_similar_wrong_procedure(tmp_path):
    _write_skill(tmp_path, "repository-overview", "Use when mapping a tool repository", ["explore"])
    _write_skill(tmp_path, "add-tool", "Use when adding a tool", ["create-tool"])
    selector = SkillSelector(SkillRegistry(builtin_dir=tmp_path), embed_fn=_fake_embed, min_similarity=0.0)

    picked = await selector.select("Give me an overview of the tool repository")

    assert [skill.name for skill in picked] == ["repository-overview"]


async def test_intent_filter_preserves_multiple_intents(tmp_path):
    _write_skill(tmp_path, "explore", "Explore the repository", ["explore"])
    _write_skill(tmp_path, "implement", "Implement the requested change", ["implement"])

    async def embed(texts: list[str]) -> list[list[float]]:
        return [[float("explore" in text.lower()), float("implement" in text.lower())] for text in texts]

    selector = SkillSelector(SkillRegistry(builtin_dir=tmp_path), embed_fn=embed, min_similarity=0.0)
    picked = await selector.select("Explore the repository and implement the requested change")

    assert {skill.name for skill in picked} == {"explore", "implement"}


@pytest.mark.parametrize(
    ("prompt", "intent", "expected"),
    [
        ("Create a flow for reviewing job applications", "create-flow", "flow-author"),
        ("Create a skill for recurring interview prep", "create-skill", "skill-author"),
        ("Create an agent for tracking applications", "create-agent", "agent-author"),
        ("Run this every weekday at nine", "create-schedule", "schedule-author"),
    ],
)
async def test_self_extension_prompts_select_the_matching_authoring_skill(tmp_path, prompt, intent, expected):
    _write_skill(tmp_path, "flow-author", "Use when authoring an automation", ["create-flow"])
    _write_skill(tmp_path, "skill-author", "Use when authoring guidance", ["create-skill"])
    _write_skill(tmp_path, "agent-author", "Use when authoring a specialist", ["create-agent"])
    _write_skill(tmp_path, "schedule-author", "Use when scheduling recurring work", ["create-schedule"])

    async def embed(texts: list[str]) -> list[list[float]]:
        return [[1.0] for _ in texts]

    selector = SkillSelector(SkillRegistry(builtin_dir=tmp_path), embed_fn=embed, min_similarity=0.0)
    picked = await selector.select(prompt)

    assert intent in picked[0].intents
    assert picked[0].name == expected
