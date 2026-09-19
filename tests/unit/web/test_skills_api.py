from __future__ import annotations

import pytest
from fastapi import HTTPException

from orchestrator.api_context import ApiServices, bind_services
from skills.models import SkillSource
from skills.registry import SkillRegistry
from web import api as web_api


def _document(description: str = "Original description") -> str:
    return (
        "---\n"
        "name: test-skill\n"
        f"description: {description}\n"
        "version: 1.0.0\n"
        "domains: [engineering]\n"
        "---\n"
        "\n"
        "Follow this procedure.\n"
    )


@pytest.fixture
def skill_registry(tmp_path) -> SkillRegistry:
    directory = tmp_path / "learned" / "test-skill"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(_document(), encoding="utf-8")
    registry = SkillRegistry(tmp_path / "builtin", tmp_path / "learned")
    with bind_services(ApiServices(skill_registry=registry)):
        yield registry


def test_skill_routes_are_mounted_at_the_dashboard_paths() -> None:
    paths = {route.path for route in web_api.router.routes}

    assert "/web/api/skills" in paths
    assert "/web/api/skills/{name}" in paths
    assert not any("/web/api/web/api/" in path for path in paths)


async def test_skill_api_lists_reads_updates_and_reloads(skill_registry: SkillRegistry) -> None:
    listed = await web_api.list_skills()
    assert listed[0]["name"] == "test-skill"

    updated = _document("Updated description")
    result = await web_api.update_skill("test-skill", web_api.SkillUpdate(content=updated))

    assert result["content"] == updated
    assert skill_registry.get("test-skill").description == "Updated description"


async def test_skill_api_creates_a_structured_learned_skill(tmp_path) -> None:
    registry = SkillRegistry(tmp_path / "builtin", tmp_path / "skills")
    with bind_services(ApiServices(skill_registry=registry, north_home=tmp_path)):
        result = await web_api.create_skill(web_api.SkillCreate(
            name="review-job-match",
            description="Use when reviewing whether a job matches the user.",
            instructions="Compare the role to the resume and explain the strongest evidence.",
            domains=["jobs", "review"],
        ))

    assert result["name"] == "review-job-match"
    assert result["source"] == "learned"
    created = tmp_path / "skills" / "review-job-match" / "SKILL.md"
    assert created.exists()
    assert "domains:" in created.read_text(encoding="utf-8")


async def test_skill_api_rejects_rename(skill_registry: SkillRegistry) -> None:
    renamed = _document().replace("name: test-skill", "name: renamed")

    with pytest.raises(HTTPException) as exc:
        await web_api.update_skill("test-skill", web_api.SkillUpdate(content=renamed))

    assert exc.value.status_code == 422


async def test_skill_api_does_not_persist_a_status_the_registry_would_reject(
    skill_registry: SkillRegistry,
) -> None:
    original = _document()
    invalid = original.replace("version: 1.0.0", "version: 1.0.0\nstatus: unknown")

    with pytest.raises(HTTPException) as exc:
        await web_api.update_skill("test-skill", web_api.SkillUpdate(content=invalid))

    assert exc.value.status_code == 422
    assert skill_registry.get("test-skill").description == "Original description"
    assert skill_registry.get("test-skill").directory.joinpath("SKILL.md").read_text() == original


async def test_skill_api_rejects_editing_builtin_skill(tmp_path) -> None:
    directory = tmp_path / "builtin" / "test-skill"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(_document(), encoding="utf-8")
    registry = SkillRegistry(tmp_path / "builtin")

    with bind_services(ApiServices(skill_registry=registry)), pytest.raises(HTTPException) as exc:
        await web_api.update_skill("test-skill", web_api.SkillUpdate(content=_document("Changed")))

    assert exc.value.status_code == 403


async def test_skill_api_deletes_learned_skill_but_not_builtin(tmp_path) -> None:
    builtin = tmp_path / "builtin"
    learned = tmp_path / "learned"
    builtin_skill = builtin / "builtin-skill"
    learned_skill = learned / "learned-skill"
    builtin_skill.mkdir(parents=True)
    learned_skill.mkdir(parents=True)
    builtin_doc = _document().replace("test-skill", "builtin-skill")
    learned_doc = _document().replace("test-skill", "learned-skill")
    builtin_skill.joinpath("SKILL.md").write_text(builtin_doc, encoding="utf-8")
    learned_skill.joinpath("SKILL.md").write_text(learned_doc, encoding="utf-8")
    registry = SkillRegistry(builtin, learned)

    with bind_services(ApiServices(skill_registry=registry)):
        with pytest.raises(HTTPException) as builtin_error:
            await web_api.delete_skill("builtin-skill")
        assert builtin_error.value.status_code == 403
        assert await web_api.delete_skill("learned-skill") is None

    assert not learned_skill.exists()
    assert registry.get("builtin-skill").source is SkillSource.BUILTIN
