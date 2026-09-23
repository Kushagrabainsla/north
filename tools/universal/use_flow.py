"""UseFlowTool - inspect a named flow before a runner executes it."""

from __future__ import annotations

from typing import Any

from flows.exceptions import FlowNotFoundError
from flows.registry import FlowRegistry
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class UseFlowTool(Tool):
    name = "use_flow"
    description = "Load a named declarative flow and return its ordered steps for execution or review."
    parameters_schema = {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "The flow name."}},
        "required": ["name"],
    }

    def __init__(self, registry: FlowRegistry) -> None:
        self._registry = registry

    async def run(self, input: ToolInput) -> ToolOutput:
        name = str(input.params.get("name") or "").strip()
        if not name:
            return ToolOutput(success=False, error="Parameter 'name' is required.")
        try:
            flow = self._registry.get(name)
        except FlowNotFoundError:
            available = ", ".join(sorted(self._registry.names())) or "(none)"
            return ToolOutput(success=False, error=f"Unknown flow '{name}'. Available: {available}")
        return ToolOutput(
            success=True,
            data={
                "name": flow.name,
                "description": flow.description,
                "steps": [
                    {
                        "name": step.name,
                        "skill": step.skill,
                        "instructions": step.instructions,
                        "inputs": step.inputs,
                        "approval": step.approval,
                    }
                    for step in flow.steps
                ],
            },
        )

    def format_output(self, data: dict[str, Any]) -> str:
        lines = [f"# Flow: {data.get('name')}", "", str(data.get("description") or ""), "", "Steps:"]
        for index, step in enumerate(data.get("steps") or [], start=1):
            approval = step.get("approval", "on_mutation")
            lines.append(
                f"{index}. {step.get('name')} "
                f"[skill={step.get('skill')}; approval={approval}]"
            )
            if step.get("instructions"):
                lines.append(f"   {step['instructions']}")
        return "\n".join(lines)
