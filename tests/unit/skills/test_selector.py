"""Tests for SkillSelector semantic selection, thresholding, and fallback."""

from __future__ import annotations

from pathlib import Path

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


def _write_skill(base: Path, name: str, description: str) -> None:
    directory = base / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nbody for {name}", encoding="utf-8"
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
