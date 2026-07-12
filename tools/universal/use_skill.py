"""UseSkillTool - load the full instructions for a named skill on demand.

The primary path is semantic pre-selection (skills are injected into context
before the agent acts). This tool is the fallback for the long tail: when a
listed skill fits but its body was not pre-injected, the agent can pull it in.
It only ever reads - bundled files are returned as read-only references the agent
opens itself with read_file; the skill system never executes anything.
"""

from __future__ import annotations

from typing import Any

from skills.exceptions import SkillNotFoundError
from skills.registry import SkillRegistry
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class UseSkillTool(Tool):
    """Return a named skill's step-by-step instructions and reference-file names."""

    name = "use_skill"
    description = (
        "Load the full step-by-step instructions for a named skill (a reusable "
        "procedure for a recurring kind of task). Use it when a skill listed as "
        "available fits the current task and its full body was not already provided. "
        "Returns the instructions plus the names of any read-only reference files in "
        "the skill's folder (open those with read_file)."
    )
    parameters_schema = {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "The skill's name."}},
        "required": ["name"],
    }

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry

    async def run(self, input: ToolInput) -> ToolOutput:
        name = str(input.params.get("name") or "").strip()
        if not name:
            return ToolOutput(success=False, error="Parameter 'name' is required.")
        try:
            skill = self._registry.get(name)
        except SkillNotFoundError:
            available = ", ".join(sorted(self._registry.names())) or "(none)"
            return ToolOutput(success=False, error=f"Unknown skill '{name}'. Available: {available}")
        return ToolOutput(
            success=True,
            data={
                "name": skill.name,
                "instructions": skill.body,
                "reference_files": skill.bundled_file_names(),
            },
        )

    def format_output(self, data: dict[str, Any]) -> str:
        references = data.get("reference_files") or []
        body = f"# Skill: {data.get('name', '')}\n\n{data.get('instructions', '')}"
        if references:
            body += f"\n\nReference files (read with read_file): {', '.join(references)}"
        return body
