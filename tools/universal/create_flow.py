"""CreateFlowTool - create and hot-reload declarative North flows."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import yaml

from flows.models import FLOW_FILENAME, FlowSource, flow_fingerprint
from flows.registry import FlowRegistry, parse_flow_document
from flows.store import FlowRunStore
from policies.self_edit import SelfEditPolicy
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.universal._flow_validation import validate_flow_capabilities

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
        "Create and validate a reusable declarative flow. New flows are candidates, not active promises. "
        "Before creating one, use the authoring-a-north-flow skill, reuse existing capabilities, provide "
        "an executable active skill, instructions, inputs, and approval mode for every step; then validate "
        "the candidate, test it, and only then activate it. Agents and tools belong to skill contracts."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "update", "list", "read", "validate", "activate"],
            },
            "name": {"type": "string", "description": "Lowercase flow name, e.g. job-application-review."},
            "description": {"type": "string", "description": "Outcome the flow achieves."},
            "steps": {
                "type": "array",
                "description": (
                    "Ordered skill steps. Each has name, skill, instructions, optional inputs, and approval."
                ),
                "items": {"type": "object"},
            },
            "domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Agent domains that may use the flow (default: general).",
            },
            "test_run_id": {
                "type": "string",
                "description": "Completed test-mode flow run proving this exact candidate works.",
            },
            "user_confirmed": {
                "type": "boolean",
                "description": "True only after the user explicitly approves activation.",
            },
        },
        "required": ["action"],
    }

    def __init__(
        self,
        registry: FlowRegistry,
        learned_dir: Path | None = None,
        self_edit_policy: SelfEditPolicy | None = None,
        tool_registry=None,
        skill_registry=None,
        agent_registry=None,
        flow_store: FlowRunStore | None = None,
    ) -> None:
        self._registry = registry
        self._learned_dir = learned_dir or (Path.home() / ".north" / "flows")
        self._self_edit_policy = self_edit_policy
        self._tool_registry = tool_registry
        self._skill_registry = skill_registry
        self._agent_registry = agent_registry
        self._flow_store = flow_store

    def mutates(self, params: dict[str, Any] | None = None) -> bool:
        return str((params or {}).get("action") or "").strip().lower() in {
            "create",
            "update",
            "activate",
        }

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
        if action == "validate":
            name = str(input.params.get("name") or "").strip()
            if not name:
                return ToolOutput(success=False, error="Parameter 'name' is required for validate.")
            try:
                flow = self._registry.get(name)
            except Exception as exc:
                return ToolOutput(success=False, error=str(exc))
            report = validate_flow_capabilities(
                flow,
                skill_registry=self._skill_registry,
                agent_registry=self._agent_registry,
                tool_registry=self._tool_registry,
            )
            return ToolOutput(
                success=report.valid,
                data={"name": name, **report.as_dict()},
                error=None if report.valid else "; ".join(report.errors),
            )
        if action == "activate":
            return await self._activate(input)
        if action == "update":
            return await self._update(input)
        if action != "create":
            return ToolOutput(
                success=False,
                error="Parameter 'action' must be create, update, list, read, validate, or activate.",
            )

        raw_name = str(input.params.get("name") or "").strip()
        description = str(input.params.get("description") or "").strip()
        steps = input.params.get("steps")
        if not raw_name:
            return ToolOutput(success=False, error="Parameter 'name' is required.")
        if not description:
            return ToolOutput(success=False, error="Parameter 'description' is required.")
        if not isinstance(steps, list) or not steps:
            return ToolOutput(success=False, error="Parameter 'steps' must be a non-empty list.")
        if any(isinstance(step, dict) and ({"tool", "agent"} & step.keys()) for step in steps):
            return ToolOutput(
                success=False,
                error=(
                    "Flow steps reference only skills. Move agent and tool choices into the skill's "
                    "execution contract."
                ),
            )

        name = _slug(raw_name)
        document = yaml.safe_dump(
            {
                "name": name,
                "description": description,
                "source": FlowSource.LEARNED.value,
                "status": "candidate",
                "domains": input.params.get("domains") or ["general"],
                "steps": steps,
            },
            sort_keys=False,
            allow_unicode=True,
        )
        directory = self._learned_dir / name
        path = directory / FLOW_FILENAME
        if path.exists():
            return ToolOutput(
                success=False,
                error=f"Flow '{name}' already exists. Read and update the existing definition instead.",
            )
        try:
            # Validate before writing so malformed flows never enter the registry.
            flow = parse_flow_document(document, directory, FlowSource.LEARNED)
            report = validate_flow_capabilities(
                flow,
                skill_registry=self._skill_registry,
                agent_registry=self._agent_registry,
                tool_registry=self._tool_registry,
            )
            if not report.valid:
                return ToolOutput(
                    success=False,
                    error="Flow is not executable: " + "; ".join(report.errors),
                    data=report.as_dict(),
                )
            mutation = None
            if self._self_edit_policy is not None:
                mutation = self._self_edit_policy.begin(path, "create")
            await asyncio.to_thread(_write_flow, directory, path, document)
            if mutation is not None:
                self._self_edit_policy.commit(mutation)
            self._registry.reload()
            return ToolOutput(
                success=True,
                data={
                    "name": name,
                    "path": str(path),
                    "steps": len(steps),
                    "status": "candidate",
                    "validation": report.as_dict(),
                },
            )
        except Exception as exc:
            return ToolOutput(success=False, error=f"Failed to create flow '{name}': {exc}")

    async def _update(self, input: ToolInput) -> ToolOutput:
        raw_name = str(input.params.get("name") or "").strip()
        description = str(input.params.get("description") or "").strip()
        steps = input.params.get("steps")
        if not raw_name or not description or not isinstance(steps, list) or not steps:
            return ToolOutput(
                success=False,
                error="Parameters 'name', 'description', and a non-empty 'steps' list are required for update.",
            )
        if any(isinstance(step, dict) and ({"tool", "agent"} & step.keys()) for step in steps):
            return ToolOutput(
                success=False,
                error=(
                    "Flow steps reference only skills. Move agent and tool choices into the skill's "
                    "execution contract."
                ),
            )
        name = _slug(raw_name)
        try:
            current = self._registry.get(name)
        except Exception as exc:
            return ToolOutput(success=False, error=str(exc))

        directory = self._learned_dir / name
        path = directory / FLOW_FILENAME
        document = yaml.safe_dump(
            {
                "name": name,
                "description": description,
                "source": FlowSource.LEARNED.value,
                # Every executable edit invalidates prior evidence.
                "status": "candidate",
                "domains": input.params.get("domains") or sorted(current.domains),
                "steps": steps,
            },
            sort_keys=False,
            allow_unicode=True,
        )
        try:
            flow = parse_flow_document(document, directory, FlowSource.LEARNED)
            report = validate_flow_capabilities(
                flow,
                skill_registry=self._skill_registry,
                agent_registry=self._agent_registry,
                tool_registry=self._tool_registry,
            )
            if not report.valid:
                return ToolOutput(
                    success=False,
                    error="Flow is not executable: " + "; ".join(report.errors),
                    data=report.as_dict(),
                )
            operation = "update" if path.exists() else "create"
            mutation = (
                self._self_edit_policy.begin(path, operation)
                if self._self_edit_policy is not None
                else None
            )
            await asyncio.to_thread(_write_flow, directory, path, document)
            if mutation is not None:
                self._self_edit_policy.commit(mutation)
            self._registry.reload()
            return ToolOutput(
                success=True,
                data={
                    "name": name,
                    "path": str(path),
                    "steps": len(steps),
                    "status": "candidate",
                    "updated": True,
                    "validation": report.as_dict(),
                },
            )
        except Exception as exc:
            return ToolOutput(success=False, error=f"Failed to update flow '{name}': {exc}")

    async def _activate(self, input: ToolInput) -> ToolOutput:
        name = str(input.params.get("name") or "").strip()
        test_run_id = str(input.params.get("test_run_id") or "").strip()
        if not name or not test_run_id:
            return ToolOutput(success=False, error="Parameters 'name' and 'test_run_id' are required for activate.")
        if input.params.get("user_confirmed") is not True:
            return ToolOutput(success=False, error="Activation requires explicit user confirmation.")
        if self._flow_store is None:
            return ToolOutput(success=False, error="Flow test evidence is unavailable.")
        try:
            flow = self._registry.get(name)
        except Exception as exc:
            return ToolOutput(success=False, error=str(exc))
        if flow.status not in {"candidate", "active"}:
            return ToolOutput(success=False, error=f"Flow '{name}' is {flow.status}, not a candidate.")

        report = validate_flow_capabilities(
            flow,
            skill_registry=self._skill_registry,
            agent_registry=self._agent_registry,
            tool_registry=self._tool_registry,
        )
        if not report.valid:
            return ToolOutput(success=False, error="Flow is not executable: " + "; ".join(report.errors))
        run = self._flow_store.get(test_run_id)
        if run is None or run.flow_name != name:
            return ToolOutput(success=False, error=f"No test run {test_run_id!r} exists for flow '{name}'.")
        if not run.test_mode or run.status != "completed":
            return ToolOutput(success=False, error="Activation requires a completed test-mode run.")
        fingerprint = flow_fingerprint(flow, self._skill_registry.get if self._skill_registry else None)
        if run.flow_fingerprint != fingerprint:
            return ToolOutput(
                success=False,
                error="The flow changed after it was tested; test the current candidate again.",
            )

        if flow.status == "active" and flow.activation_fingerprint == fingerprint:
            return ToolOutput(
                success=True,
                data={"name": name, "status": "active", "test_run_id": test_run_id},
            )

        path = flow.directory / FLOW_FILENAME
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return ToolOutput(success=False, error=f"Flow '{name}' is not a YAML mapping.")
            data["status"] = "active"
            data["activation_fingerprint"] = fingerprint
            document = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
            mutation = self._self_edit_policy.begin(path, "update") if self._self_edit_policy is not None else None
            await asyncio.to_thread(_write_flow, flow.directory, path, document)
            if mutation is not None:
                self._self_edit_policy.commit(mutation)
            self._registry.reload()
            return ToolOutput(
                success=True,
                data={"name": name, "status": "active", "test_run_id": test_run_id},
            )
        except Exception as exc:
            return ToolOutput(success=False, error=f"Failed to activate flow '{name}': {exc}")

    def format_output(self, data: dict[str, Any]) -> str:
        if "valid" in data:
            return (
                f"Flow '{data.get('name')}' is valid."
                if data.get("valid")
                else f"Flow '{data.get('name')}' is not valid: {'; '.join(data.get('errors') or [])}"
            )
        if data.get("status") == "active" and data.get("test_run_id"):
            return f"Flow '{data.get('name')}' activated from successful test {data.get('test_run_id')}."
        if "flows" in data:
            return "\n".join(
                f"{flow['name']} - {flow['description']} ({len(flow.get('steps', []))} steps)"
                for flow in data["flows"]
            ) or "No flows registered."
        verb = "updated" if data.get("updated") else "created"
        return (
            f"Flow candidate '{data.get('name')}' {verb} at {data.get('path')} "
            f"({data.get('steps', 0)} steps). It is not active until a test passes and it is activated."
        )


def _view(flow, *, include_steps: bool = False) -> dict[str, Any]:
    data = {
        "name": flow.name,
        "description": flow.description,
        "source": flow.source.value,
        "status": flow.status,
        "domains": sorted(flow.domains),
        "steps": len(flow.steps),
    }
    if include_steps:
        data["steps"] = [
            {
                "name": step.name,
                "skill": step.skill,
                "instructions": step.instructions,
                "inputs": step.inputs,
                "approval": step.approval,
            }
            for step in flow.steps
        ]
    return data
