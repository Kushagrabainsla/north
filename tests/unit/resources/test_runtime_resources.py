from __future__ import annotations

from utils.prompts import load_prompt
from utils.runtime_resources import builtin_skills_dir, policies_dir, web_dist_dir


def test_packaged_resource_directories_exist() -> None:
    assert builtin_skills_dir().is_dir()
    assert policies_dir().is_dir()
    assert web_dist_dir().is_dir()


def test_prompt_loader_keeps_legacy_prompt_names() -> None:
    assert "planner" in load_prompt("prompts/planner.md").lower()
