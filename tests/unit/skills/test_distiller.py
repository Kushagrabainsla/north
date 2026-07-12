"""Tests for the SkillDistiller: clustering, distillation, writing, and idempotency."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from skills.distiller import SkillDistiller, _cluster_by_similarity, _slug
from skills.registry import SkillRegistry
from skills.selector import SkillSelector


class _FakeEpisodicStore:
    def __init__(self, episodes: list[tuple[str, str, list[float] | None]]) -> None:
        self._episodes = episodes

    async def list_successful(self, domains):
        return self._episodes


class _FakeInference:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        return SimpleNamespace(text=self._text)


def _make_distiller(tmp_path: Path, episodes, distilled_json: str):
    builtin, learned = tmp_path / "builtin", tmp_path / "learned"
    builtin.mkdir()
    registry = SkillRegistry(builtin_dir=builtin, learned_dir=learned)
    selector = SkillSelector(registry, embed_fn=None)
    distiller = SkillDistiller(
        episodic_store=_FakeEpisodicStore(episodes),
        inference_router=_FakeInference(distilled_json),
        skill_registry=registry,
        skill_selector=selector,
        learned_dir=learned,
    )
    return distiller, registry, learned


def test_slug_is_filesystem_safe():
    assert _slug("Fix Parser Bugs!") == "fix-parser-bugs"


def test_cluster_requires_recurrence():
    episodes = [("t1", "fix a bug", [1.0, 0.0]), ("t2", "unrelated work", [0.0, 1.0])]
    assert _cluster_by_similarity(episodes) == []


def test_cluster_groups_similar_episodes():
    episodes = [
        ("t1", "fix bug in parser", [1.0, 0.0]),
        ("t2", "fix parser bug", [0.99, 0.01]),
        ("t3", "unrelated", [0.0, 1.0]),
    ]
    clusters = _cluster_by_similarity(episodes)
    assert len(clusters) == 1
    assert {task_id for task_id, _ in clusters[0]} == {"t1", "t2"}


_VALID_SKILL = (
    '{"name": "fix-parser-bugs", "description": "Use when a parser test is failing", '
    '"body": "1. reproduce the failing test 2. fix the parser 3. add a regression test"}'
)


async def test_distills_and_writes_learned_skill(tmp_path):
    episodes = [("t1", "fix bug in parser", [1.0, 0.0]), ("t2", "fix parser bug", [1.0, 0.0])]
    distiller, registry, learned = _make_distiller(tmp_path, episodes, _VALID_SKILL)

    written = await distiller.run_once()

    assert written == 1
    assert "fix-parser-bugs" in registry.names()
    skill = registry.get("fix-parser-bugs")
    assert set(skill.provenance) == {"t1", "t2"}
    assert (learned / "fix-parser-bugs" / "SKILL.md").exists()


async def test_idempotent_via_provenance_overlap(tmp_path):
    episodes = [("t1", "fix bug in parser", [1.0, 0.0]), ("t2", "fix parser bug", [1.0, 0.0])]
    distiller, _, _ = _make_distiller(tmp_path, episodes, _VALID_SKILL)
    assert await distiller.run_once() == 1
    assert await distiller.run_once() == 0  # same tasks already distilled -> no duplicate


async def test_null_distillation_writes_nothing(tmp_path):
    episodes = [("t1", "a task", [1.0, 0.0]), ("t2", "a similar task", [1.0, 0.0])]
    distiller, registry, _ = _make_distiller(tmp_path, episodes, '{"skill": null}')
    assert await distiller.run_once() == 0
    assert registry.names() == []


async def test_single_success_is_not_distilled(tmp_path):
    episodes = [("t1", "a lone task", [1.0, 0.0])]  # only one - below MIN_CLUSTER_SIZE
    distiller, registry, _ = _make_distiller(tmp_path, episodes, _VALID_SKILL)
    assert await distiller.run_once() == 0
    assert registry.names() == []


_TRICKY_SKILL = (
    '{"name": "handle-config", "description": "Use when: parsing #config values or edge: cases", '
    '"body": "1. reproduce the failure 2. fix the parser 3. add an edge-case regression test"}'
)


async def test_learned_skill_with_yaml_special_chars_loads_and_stays_idempotent(tmp_path):
    # A model description with ': ' and '#' must not corrupt the written frontmatter,
    # or the skill fails to load, provenance is lost, and the cluster re-distills forever.
    episodes = [("t1", "fix config parsing", [1.0, 0.0]), ("t2", "fix config parse bug", [1.0, 0.0])]
    distiller, registry, _ = _make_distiller(tmp_path, episodes, _TRICKY_SKILL)

    assert await distiller.run_once() == 1
    skill = registry.get("handle-config")
    assert "Use when:" in skill.description  # colon preserved
    assert "#config" in skill.description  # hash not treated as a YAML comment
    assert set(skill.provenance) == {"t1", "t2"}
    assert await distiller.run_once() == 0  # provenance recorded -> not re-distilled
