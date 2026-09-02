"""CreateSkillTool - create and hot-reload procedural skills at runtime.

Allows agents to package recurring procedures into reusable skills on the fly.
Writes SKILL.md to North's learned skills directory and reloads the SkillRegistry.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from skills.models import SKILL_FILENAME, SkillSource
from tools.base import Tool
from tools.models import ToolInput, ToolOutput

if TYPE_CHECKING:
    from skills.registry import SkillRegistry

_SLUG_RE = re.compile(r"[^a-z0-9_]+")


def _slug(name: str) -> str:
    s = _SLUG_RE.sub("-", name.lower().strip()).strip("-")
    return s or "skill"


def _write_skill_file(skill_dir: Path, skill_file: Path, document: str) -> None:
    """Create the skill directory and write its SKILL.md. Blocking - use to_thread."""
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file.write_text(document, encoding="utf-8")


class CreateSkillTool(Tool):
    """Create a new procedural skill (reusable step-by-step workflow) at runtime."""

    name = "create_skill"
    is_mutating = True
    description = (
        "Create a new procedural skill (a reusable step-by-step procedure for a recurring task) "
        "at runtime. Takes 'name' (kebab-case identifier), 'description' (trigger condition starting "
        "with 'Use when...'), and 'instructions' (markdown step-by-step instructions). "
        "The new skill is immediately reloaded and available via use_skill."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Skill name (e.g. 'deploy-docker-container', 'debug-auth-error')",
            },
            "description": {
                "type": "string",
                "description": (
                    "Trigger condition starting with 'Use when...'. "
                    "E.g. 'Use when building and deploying a Docker container.'"
                ),
            },
            "instructions": {
                "type": "string",
                "description": "Markdown step-by-step instructions and best practices for completing this procedure.",
            },
        },
        "required": ["name", "description", "instructions"],
    }

    def __init__(self, registry: SkillRegistry, learned_dir: Path | None = None) -> None:
        self._registry = registry
        self._learned_dir = learned_dir or (Path.home() / ".north" / "learned_skills")

    async def run(self, input: ToolInput) -> ToolOutput:
        raw_name = str(input.params.get("name") or "").strip()
        description = str(input.params.get("description") or "").strip()
        instructions = str(input.params.get("instructions") or "").strip()

        if not raw_name:
            return ToolOutput(success=False, error="Parameter 'name' is required.")
        if not description:
            return ToolOutput(success=False, error="Parameter 'description' is required.")
        if not instructions:
            return ToolOutput(success=False, error="Parameter 'instructions' is required.")

        slug_name = _slug(raw_name)
        skill_dir = self._learned_dir / slug_name
        skill_file = skill_dir / SKILL_FILENAME

        frontmatter = yaml.safe_dump(
            {
                "name": slug_name,
                "description": description,
                "source": SkillSource.LEARNED.value,
            },
            sort_keys=False,
            allow_unicode=True,
        )
        document = f"---\n{frontmatter}---\n\n{instructions}\n"

        try:
            # mkdir + write off-thread so the agent loop is never blocked on disk.
            await asyncio.to_thread(_write_skill_file, skill_dir, skill_file, document)
            if hasattr(self._registry, "reload"):
                self._registry.reload()
            return ToolOutput(
                success=True,
                data={
                    "name": slug_name,
                    "description": description,
                    "path": str(skill_file),
                },
            )
        except Exception as exc:
            return ToolOutput(success=False, error=f"Failed to create skill '{slug_name}': {exc}")

    def format_output(self, data: dict[str, Any]) -> str:
        return f"Successfully created skill '{data.get('name')}' at {data.get('path')}."
