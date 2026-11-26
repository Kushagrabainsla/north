"""UseSkillTool - load the full instructions for a named skill on demand.

A task's context lists every skill of its domain by name and description; the
task model calls this tool to load the one that fits. It only ever reads -
bundled files are returned as read-only references the agent reads itself with
a shell command; the skill system never executes anything.
"""

from __future__ import annotations

from typing import Any

from skills.exceptions import SkillNotFoundError
from skills.registry import SkillRegistry
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class UseSkillTool(Tool):
    """Return a named skill's step-by-step instructions and its reference files."""

    name = "use_skill"
    description = (
        "Load the full step-by-step instructions for a named skill (a reusable "
        "procedure for a recurring kind of task). Use it when a skill in the list of "
        "skills fits the current task, before you act. "
        "Returns the instructions plus the folder and names of any read-only reference "
        "files in the skill's folder (read those with bash, e.g. `cat <folder>/<file>`)."
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
                "folder": str(skill.directory),
                "reference_files": skill.bundled_file_names(),
            },
        )

    def format_output(self, data: dict[str, Any]) -> str:
        references = data.get("reference_files") or []
        body = f"# Skill: {data.get('name', '')}\n\n{data.get('instructions', '')}"
        if references:
            # Names alone could not be found: the folder is where they are.
            folder = data.get("folder", "")
            body += f"\n\nReference files in {folder} (read with bash, e.g. `cat`): {', '.join(references)}"
        return body
