from __future__ import annotations

import pytest
from fastapi import HTTPException

from orchestrator.api_context import ApiServices, bind_services
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
    directory = tmp_path / "test-skill"
    directory.mkdir()
    (directory / "SKILL.md").write_text(_document(), encoding="utf-8")
    registry = SkillRegistry(tmp_path)
    with bind_services(ApiServices(skill_registry=registry)):
        yield registry


async def test_skill_api_lists_reads_updates_and_reloads(skill_registry: SkillRegistry) -> None:
    listed = await web_api.list_skills()
    assert listed[0]["name"] == "test-skill"

    updated = _document("Updated description")
    result = await web_api.update_skill("test-skill", web_api.SkillUpdate(content=updated))

    assert result["content"] == updated
    assert skill_registry.get("test-skill").description == "Updated description"


async def test_skill_api_rejects_rename(skill_registry: SkillRegistry) -> None:
    renamed = _document().replace("name: test-skill", "name: renamed")

    with pytest.raises(HTTPException) as exc:
        await web_api.update_skill("test-skill", web_api.SkillUpdate(content=renamed))

    assert exc.value.status_code == 422
