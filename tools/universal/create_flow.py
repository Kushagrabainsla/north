"""CreateFlowTool - create and hot-reload declarative North flows.

One card per change: creating, updating or activating a flow is asked about
once, and that approval covers one test run and switching the flow on. Each
step of the test still asks as usual. A new flow that fails its test stays a
draft; an edit that fails goes back to the version that was live.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from flows.models import FLOW_FILENAME, FlowSource, flow_fingerprint
from flows.registry import FlowRegistry, parse_flow_document
from flows.store import FlowRun, FlowRunStore
from flows.validation import validate_flow_capabilities
from policies.self_edit import SelfEditPolicy
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.universal._procedure_edit import begin_edit, describe_user_edit
from tools.universal.flow_schedules import FlowSchedules, describe_result, describe_schedule

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _slug(name: str) -> str:
    value = _SLUG_RE.sub("-", name.lower().strip()).strip("-")
    return value or "flow"


def _write_flow(directory: Path, path: Path, document: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")


class CreateFlowTool(Tool):
    """Create a validated declarative workflow at runtime, test it once, and switch it on."""

    name = "create_flow"
    is_mutating = True
    starts_runs = True  # its runs' steps take the workspace lock; see `Tool.starts_runs`
    description = (
        "Create, update, inspect, activate or schedule a reusable declarative flow. Before creating one, use the "
        "authoring-a-north-flow skill, resolve missing setup choices with ask_user in one question, including "
        "explicit manual-only, "
        "one-time, or recurring timing (never assume), reuse capabilities, take the timezone only from North "
        "settings (never ask for it or declare a timezone input), and provide an executable skill, instructions, "
        "inputs, and approval mode for every step. create, update and activate are each asked about once; the flow "
        "is then run once as a test (pass test_inputs for the smallest safe case) and is live if the test completes. "
        "A failing new flow stays a draft; a failing edit goes back to the live version. Agents and tools belong to "
        "skill contracts. A schedule is a property of a flow: list and read show each flow's schedules and next "
        "runs; collecting timing does not install a schedule or authorize activation, so schedule a flow only "
        "after it is live and the user confirmed. 'schedule' runs flow 'name' later. A reminder is a schedule of the "
        "built-in 'reminder' flow: pass 'message', the text north delivers at that time as it is, with no test; "
        "the result says where it will appear - tell the user exactly that. Give a short title in 'label'. Times "
        "are the USER'S LOCAL TIME - pass the hour they said, never convert it, never pass a timezone. Pass "
        "exactly one of: run_at 'YYYY-MM-DDTHH:MM' for one run; interval_minutes for every N minutes; or hour "
        "(0-23) with optional minute and days (day names, numbers 0=Mon..6=Sun, or 'weekdays' / 'weekends' / "
        "'daily'), so \"every weekday at 9:30\" is hour 9, minute 30, days 'weekdays'. 'reschedule' changes "
        "the schedule named in 'schedule' (its time, days, label, or enabled false to pause and true to "
        "resume); pass only what changes. 'unschedule' removes the schedule named in 'schedule', or the "
        "one-shot 'job_id', for good; a built-in one goes back to its shipped settings. What a schedule does "
        "lives in its flow, so change the flow, not the schedule. List first when the user asks what is "
        "scheduled or to change or stop something."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "update", "activate", "list", "read", "schedule", "reschedule", "unschedule"],
                "description": "activate = test a draft flow as it is now and switch it on.",
            },
            "message": {
                "type": "string",
                "description": "schedule only, for a reminder: the text to deliver ('Take the laundry out.').",
            },
            "label": {"type": "string", "description": "A schedule's short title, 2-4 words, shown in lists."},
            "run_at": {"type": "string", "description": "Local 'YYYY-MM-DDTHH:MM' for a single run."},
            "hour": {"type": "integer", "description": "Hour (0-23), local, for a repeating schedule."},
            "minute": {"type": "integer", "description": "Minute (0-59, default 0)."},
            "interval_minutes": {"type": "integer", "description": "Run every N minutes (minimum 1)."},
            "days": {
                "description": (
                    "Which days: a list of day names or numbers (0=Mon..6=Sun), or 'weekdays' / 'weekends' / "
                    "'daily'. Omit for every day; on reschedule, 'daily' clears a day restriction."
                ),
            },
            "enabled": {"type": "boolean", "description": "reschedule only: false pauses, true resumes."},
            "schedule": {
                "type": "string",
                "description": "reschedule / unschedule: the schedule's name, as list or read shows it.",
            },
            "job_id": {"type": "string", "description": "unschedule only: a one-shot run's job id."},
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
            "test_inputs": {
                "type": "object",
                "description": "Inputs for the one test run, as ${inputs.key}: the smallest safe case.",
            },
            "test_run_id": {
                "type": "string",
                "description": "activate only: a completed test run of this exact flow to use instead of a new one.",
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
        runner=None,
        schedules: FlowSchedules | None = None,
    ) -> None:
        self._registry = registry
        self._schedules = schedules
        from config.settings import settings

        self._learned_dir = learned_dir or (settings.north_home / "flows")
        self._self_edit_policy = self_edit_policy
        self._tool_registry = tool_registry
        self._skill_registry = skill_registry
        self._agent_registry = agent_registry
        self._flow_store = flow_store
        # tools.universal.flow_runner.FlowRunner: runs the one test run.
        self._runner = runner

    def mutates(self, params: dict[str, Any] | None = None) -> bool:
        # Adding a schedule asks nobody, as before: the flow it runs was approved when it went live.
        action = str((params or {}).get("action") or "").strip().lower()
        return action in {"create", "update", "activate", "reschedule", "unschedule"}

    async def describe(self, input: ToolInput):
        request = await super().describe(input)
        action = str(input.params.get("action") or "").strip().lower()
        name = _slug(str(input.params.get("name") or ""))
        outcome = {
            "create": f"creates the flow '{name}', runs it once as a test now",
            "update": f"updates the flow '{name}', runs it once as a test now",
            "activate": f"runs the flow '{name}' once as a test now",
        }.get(action)
        if outcome:
            if action == "activate" and input.params.get("test_run_id"):
                outcome = f"switches on the flow '{name}' on the evidence of test run {input.params['test_run_id']}"
            request = replace(
                request,
                message=(
                    f"Approving {outcome} (each step still asks as usual), and switches it on if the test "
                    "completes. If it fails, a new flow stays a draft and an edited one goes back to the "
                    "version that was live.\n\n" + request.message
                ),
            )
        if action == "update":
            path = self._learned_dir / name / FLOW_FILENAME
            return describe_user_edit(self._self_edit_policy, path, input, request)
        return request

    async def run(self, input: ToolInput) -> ToolOutput:
        action = str(input.params.get("action") or "").strip().lower()
        if action == "list":
            schedules = await self._schedules_by_flow()
            return ToolOutput(
                success=True,
                data={
                    "flows": [_view(flow, schedules=schedules) for flow in self._registry.all()],
                    "timezone": FlowSchedules.timezone(),
                },
            )
        if action == "read":
            name = str(input.params.get("name") or "").strip()
            if not name:
                return ToolOutput(success=False, error="Parameter 'name' is required for read.")
            try:
                flow = self._registry.get(name)
            except Exception as exc:
                return ToolOutput(success=False, error=str(exc))
            return ToolOutput(
                success=True, data=_view(flow, include_steps=True, schedules=await self._schedules_by_flow())
            )
        if action in {"schedule", "reschedule", "unschedule"}:
            return await self._schedule(action, input.params)
        if action == "activate":
            return await self._activate(input)
        if action not in {"create", "update"}:
            return ToolOutput(
                success=False,
                error=(
                    "Parameter 'action' must be create, update, activate, list, read, schedule, reschedule, "
                    "or unschedule."
                ),
            )
        return await self._write_and_prove(input, action)

    async def _schedules_by_flow(self) -> dict[str, list[dict[str, Any]]] | None:
        return await self._schedules.by_flow() if self._schedules is not None else None

    async def _schedule(self, action: str, params: dict[str, Any]) -> ToolOutput:
        """A flow's schedules: add one, change one, or remove one."""
        if self._schedules is None:
            return ToolOutput(success=False, error="Scheduling is unavailable here.")
        if action == "schedule":
            return await self._schedules.add({**params, "flow": str(params.get("name") or "").strip()})
        if action == "reschedule":
            return await self._schedules.change(params)
        return await self._schedules.remove(params)

    async def _write_and_prove(self, input: ToolInput, action: str) -> ToolOutput:
        """Write the flow, test it once, and switch it on; on failure keep what was live."""
        raw_name = str(input.params.get("name") or "").strip()
        description = str(input.params.get("description") or "").strip()
        steps = input.params.get("steps")
        if not raw_name or not description or not isinstance(steps, list) or not steps:
            return ToolOutput(
                success=False,
                error="Parameters 'name', 'description', and a non-empty 'steps' list are required.",
            )
        if any(isinstance(step, dict) and ({"tool", "agent"} & step.keys()) for step in steps):
            return ToolOutput(
                success=False,
                error=(
                    "Flow steps reference only skills. Move agent and tool choices into the skill's execution contract."
                ),
            )
        name = _slug(raw_name)
        directory = self._learned_dir / name
        path = directory / FLOW_FILENAME
        current = None
        if action == "create" and path.exists():
            return ToolOutput(
                success=False,
                error=f"Flow '{name}' already exists. Read and update the existing definition instead.",
            )
        if action == "update":
            try:
                current = self._registry.get(name)
            except Exception as exc:
                return ToolOutput(success=False, error=str(exc))
        document = yaml.safe_dump(
            {
                "name": name,
                "description": description,
                "source": FlowSource.LEARNED.value,
                # A draft until its test completes; every executable edit needs new evidence.
                "status": "candidate",
                "domains": input.params.get("domains") or (sorted(current.domains) if current else ["general"]),
                "steps": steps,
            },
            sort_keys=False,
            allow_unicode=True,
        )
        try:
            # Validate before writing so malformed flows never enter the registry.
            flow = parse_flow_document(document, directory, FlowSource.LEARNED)
        except Exception as exc:
            return ToolOutput(success=False, error=f"Flow is invalid: {exc}")
        report = validate_flow_capabilities(
            flow,
            skill_registry=self._skill_registry,
            agent_registry=self._agent_registry,
            tool_registry=self._tool_registry,
            allow_candidate_skills=True,
        )
        if not report.valid:
            return ToolOutput(
                success=False, error="Flow is not executable: " + "; ".join(report.errors), data=report.as_dict()
            )
        if self._runner is None:
            return ToolOutput(success=False, error="Flow testing is unavailable, so nothing changed.")

        previous = path.read_text(encoding="utf-8") if path.exists() else None
        try:
            operation = "update" if previous is not None else "create"
            mutation = begin_edit(self._self_edit_policy, path, operation, input) if self._self_edit_policy else None
            await asyncio.to_thread(_write_flow, directory, path, document)
            if mutation is not None:
                self._self_edit_policy.commit(mutation)
            self._registry.reload()
        except Exception as exc:
            return ToolOutput(success=False, error=f"Failed to write flow '{name}': {exc}")

        run = await self._test(name, input)
        if run.status == "completed":
            return await self._switch_on(name, run, updated=action == "update", steps=len(steps))
        if previous is not None:
            # The edit failed its test: what was live stays live.
            await asyncio.to_thread(_write_flow, directory, path, previous)
            self._registry.reload()
            if run.status in {"paused", "waiting"}:
                self._runner.discard(run.run_id)
            kept = "The previous version is live again."
        else:
            kept = "The new flow stays a draft."
        return ToolOutput(
            success=False,
            error=f"The test run ended {run.status}: {run.error or 'no error recorded'}. {kept}",
            data={"name": name, "test_run_id": run.run_id, "status": run.status},
        )

    async def _test(self, name: str, input: ToolInput) -> FlowRun:
        inputs = input.params.get("test_inputs")
        return await self._runner.run(
            name,
            task_id=str(input.params.get("task_id") or ""),
            inputs=inputs if isinstance(inputs, dict) else None,
            test_mode=True,
            trigger="test",
        )

    async def _activate(self, input: ToolInput) -> ToolOutput:
        """Switch on a draft flow: on a completed test run named in test_run_id, or one run now."""
        name = str(input.params.get("name") or "").strip()
        if not name:
            return ToolOutput(success=False, error="Parameter 'name' is required for activate.")
        try:
            flow = self._registry.get(name)
        except Exception as exc:
            return ToolOutput(success=False, error=str(exc))
        if flow.status not in {"candidate", "active"}:
            return ToolOutput(success=False, error=f"Flow '{name}' is {flow.status}, not a draft.")
        report = validate_flow_capabilities(
            flow,
            skill_registry=self._skill_registry,
            agent_registry=self._agent_registry,
            tool_registry=self._tool_registry,
        )
        if not report.valid:
            return ToolOutput(success=False, error="Flow is not executable: " + "; ".join(report.errors))
        if test_run_id := str(input.params.get("test_run_id") or "").strip():
            if self._flow_store is None:
                return ToolOutput(success=False, error="Flow test evidence is unavailable.")
            run = self._flow_store.get(test_run_id)
            if run is None or run.flow_name != name:
                return ToolOutput(success=False, error=f"No test run {test_run_id!r} exists for flow '{name}'.")
        elif self._runner is None:
            return ToolOutput(success=False, error="Flow testing is unavailable.")
        else:
            run = await self._test(name, input)
        if not run.test_mode or run.status != "completed":
            return ToolOutput(
                success=False,
                error=f"Activation needs a completed test run; this one ended {run.status}: {run.error or 'no error'}.",
                data={"name": name, "test_run_id": run.run_id, "status": run.status},
            )
        return await self._switch_on(name, run)

    async def _switch_on(self, name: str, run: FlowRun, *, updated: bool = False, steps: int = 0) -> ToolOutput:
        """Mark *name* live on the evidence of *run*, a completed test of exactly this version."""
        flow = self._registry.get(name)
        fingerprint = flow_fingerprint(flow, self._skill_registry.get if self._skill_registry else None)
        if run.flow_fingerprint != fingerprint:
            return ToolOutput(
                success=False, error="The flow changed after it was tested; test the current version again."
            )
        live = {"name": name, "status": "active", "test_run_id": run.run_id, "updated": updated, "steps": steps}
        if flow.status == "active" and flow.activation_fingerprint == fingerprint:
            return ToolOutput(success=True, data=live)
        path = flow.directory / FLOW_FILENAME
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return ToolOutput(success=False, error=f"Flow '{name}' is not a YAML mapping.")
            data["status"] = "active"
            data["activation_fingerprint"] = fingerprint
            document = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
            mutation = self._self_edit_policy.begin(path, "activate") if self._self_edit_policy is not None else None
            await asyncio.to_thread(_write_flow, flow.directory, path, document)
            if mutation is not None:
                self._self_edit_policy.commit(mutation)
            self._registry.reload()
            return ToolOutput(success=True, data=live)
        except Exception as exc:
            return ToolOutput(success=False, error=f"Failed to switch on flow '{name}': {exc}")

    def format_output(self, data: dict[str, Any]) -> str:
        if data.get("status") == "active" and data.get("test_run_id"):
            state = "updated and live" if data.get("updated") else "live"
            return f"Flow '{data.get('name')}' is {state}: test run {data.get('test_run_id')} completed."
        if {"cancelled", "changed", "runs_at"} & data.keys() or data.get("type") == "recurring":
            return describe_result(data)
        if "flows" in data:
            lines = []
            for flow in data["flows"]:
                lines.append(f"{flow['name']} - {flow['description']} ({flow['steps']} steps; {flow['status']})")
                lines += [f"    {describe_schedule(view)}" for view in flow.get("schedules", [])]
            if data.get("timezone") and any(flow.get("schedules") for flow in data["flows"]):
                lines.append(f"(times in {data['timezone']})")
            return "\n".join(lines) or "No flows registered."
        if isinstance(data.get("steps"), list):
            return f"Flow '{data['name']}' ({data['status']}; {len(data['steps'])} steps):\n" + yaml.safe_dump(
                data, sort_keys=False, allow_unicode=True
            )
        return str(data)


def _view(
    flow, *, include_steps: bool = False, schedules: dict[str, list[dict[str, Any]]] | None = None
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": flow.name,
        "description": flow.description,
        "source": flow.source.value,
        "status": flow.status,
        "domains": sorted(flow.domains),
        "steps": len(flow.steps),
    }
    if schedules is not None:
        data["schedules"] = schedules.get(flow.name, [])
    if include_steps:
        data["steps"] = [
            {
                "name": step.name,
                "skill": step.skill,
                "instructions": step.instructions,
                "inputs": step.inputs,
                "approval": step.approval,
                "action": step.action,
            }
            for step in flow.steps
        ]
    return data
