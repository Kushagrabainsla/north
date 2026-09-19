"""CreateFlowTool - create and hot-reload declarative North flows."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import yaml

from flows.models import FLOW_FILENAME, FlowSource
from flows.registry import FlowRegistry, parse_flow_document
from policies.self_edit import SelfEditPolicy
from tools.base import Tool
from tools.models import ToolInput, ToolOutput

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _slug(name: str) -> str:
    value = _SLUG_RE.sub("-", name.lower().strip()).strip("-")
    return value or "flow"


def _write_flow(directory: Path, path: Path, document: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")


class CreateFlowTool(Tool):
    """Create a validated declarative workflow at runtime."""

    name = "create_flow"
    is_mutating = True
    description = (
        "Create a reusable declarative flow: an ordered list of allowlisted North tool steps. "
        "Flows describe orchestration; they do not execute arbitrary code."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "list", "read"]},
            "name": {"type": "string", "description": "Lowercase flow name, e.g. job-application-review."},
            "description": {"type": "string", "description": "Outcome the flow achieves."},
            "steps": {
                "type": "array",
                "description": "Ordered steps. Each has name, tool, optional params, skill, approval, and description.",
                "items": {"type": "object"},
            },
        },
        "required": ["action"],
    }

    def __init__(
        self,
        registry: FlowRegistry,
        learned_dir: Path | None = None,
        self_edit_policy: SelfEditPolicy | None = None,
    ) -> None:
        self._registry = registry
        self._learned_dir = learned_dir or (Path.home() / ".north" / "flows")
        self._self_edit_policy = self_edit_policy

    async def run(self, input: ToolInput) -> ToolOutput:
        action = str(input.params.get("action") or "").strip().lower()
        if action == "list":
            return ToolOutput(success=True, data={"flows": [_view(flow) for flow in self._registry.all()]})
        if action == "read":
            name = str(input.params.get("name") or "").strip()
            if not name:
                return ToolOutput(success=False, error="Parameter 'name' is required for read.")
            try:
                return ToolOutput(success=True, data=_view(self._registry.get(name), include_steps=True))
            except Exception as exc:
                return ToolOutput(success=False, error=str(exc))
        if action != "create":
            return ToolOutput(success=False, error="Parameter 'action' must be create, list, or read.")

        raw_name = str(input.params.get("name") or "").strip()
        description = str(input.params.get("description") or "").strip()
        steps = input.params.get("steps")
        if not raw_name:
            return ToolOutput(success=False, error="Parameter 'name' is required.")
        if not description:
            return ToolOutput(success=False, error="Parameter 'description' is required.")
        if not isinstance(steps, list) or not steps:
            return ToolOutput(success=False, error="Parameter 'steps' must be a non-empty list.")

        name = _slug(raw_name)
        document = yaml.safe_dump(
            {"name": name, "description": description, "source": FlowSource.LEARNED.value, "steps": steps},
            sort_keys=False,
            allow_unicode=True,
        )
        directory = self._learned_dir / name
        path = directory / FLOW_FILENAME
        try:
            # Validate before writing so malformed flows never enter the registry.
            parse_flow_document(document, directory, FlowSource.LEARNED)
            mutation = None
            if self._self_edit_policy is not None:
                mutation = self._self_edit_policy.begin(path, "create")
            await asyncio.to_thread(_write_flow, directory, path, document)
            if mutation is not None:
                self._self_edit_policy.commit(mutation)
            self._registry.reload()
            return ToolOutput(success=True, data={"name": name, "path": str(path), "steps": len(steps)})
        except Exception as exc:
            return ToolOutput(success=False, error=f"Failed to create flow '{name}': {exc}")

    def format_output(self, data: dict[str, Any]) -> str:
        if "flows" in data:
            return "\n".join(
                f"{flow['name']} - {flow['description']} ({len(flow.get('steps', []))} steps)"
                for flow in data["flows"]
            ) or "No flows registered."
        return f"Flow '{data.get('name')}' created at {data.get('path')} ({data.get('steps', 0)} steps)."


def _view(flow, *, include_steps: bool = False) -> dict[str, Any]:
    data = {
        "name": flow.name,
        "description": flow.description,
        "source": flow.source.value,
        "version": flow.version,
        "status": flow.status,
        "domains": sorted(flow.domains),
        "steps": len(flow.steps),
    }
    if include_steps:
        data["steps"] = [
            {
                "name": step.name,
                "tool": step.tool,
                "params": step.params,
                "skill": step.skill,
                "approval": step.approval,
                "description": step.description,
            }
            for step in flow.steps
        ]
    return data
