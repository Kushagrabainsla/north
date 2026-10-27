"""Dashboard management views over existing skills, flows and user settings."""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from approval.models import Card
from config.browser_profiles import BrowserProfile, SetupProgress, discover_browser_profiles
from flows.exceptions import FlowNotFoundError, FlowParseError
from flows.models import FLOW_FILENAME, Flow, FlowSource, flow_fingerprint
from flows.registry import parse_flow_document
from flows.store import FlowRun
from flows.validation import schedulable_flow_error, validate_flow_capabilities
from inference.registry import PROVIDER_DEFINITIONS
from jobs.models import JobStatus, late_note
from orchestrator.api_context import current_services
from policies.self_edit import SelfEditPolicy
from skills.exceptions import SkillNotFoundError, SkillParseError
from skills.models import SKILL_FILENAME, SkillSource
from skills.parser import parse_skill_document
from skills.registry import parse_execution_contract, rejection_reason
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.universal.cancel_schedule import CancelScheduleTool
from tools.universal.create_flow import CreateFlowTool
from tools.universal.create_tool import (
    _candidate_root,
    _check_code_safety,
    _find_candidate_path,
    _find_tool_path,
    _list_tools,
    _render_stub,
)
from tools.universal.flow_runner import STOPPED, step_approval_title
from tools.universal.schedule_task import ScheduleTaskTool
from tools.universal.update_schedule import UpdateScheduleTool
from utils.ids import generate_id
from utils.tasks import spawn

# This router is composed into ``web.api.router``, which owns the public
# ``/web/api`` prefix and request-level dependencies. Repeating either here
# silently mounts these endpoints at ``/web/api/web/api/...``.
router = APIRouter(tags=["web"])


class SkillUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)


class SkillCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=2_000)
    instructions: str = Field(min_length=1, max_length=8_000)
    domains: list[str] = Field(default_factory=lambda: ["general"])
    executor: str = "general"
    tools: list[str] = Field(default_factory=list)
    approval: str = Field(default="never", pattern="^(never|on_mutation|before_step|always)$")
    inputs: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    outputs: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    success_criteria: list[str] = Field(
        default_factory=lambda: ["The requested procedure completed and returned verifiable evidence."]
    )


class SkillDuplicate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class ToolCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=2_000)
    tool_type: str = Field(default="specialized", pattern="^(universal|specialized)$")


class ToolUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)


class FlowUpdate(BaseModel):
    content: str | None = Field(default=None, max_length=100_000)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2_000)
    domains: list[str] = Field(default_factory=lambda: ["general"])
    status: str = "candidate"
    steps: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def has_content_or_definition(self):
        if not self.content and (not self.name or not self.description or not self.steps):
            raise ValueError("Provide content or a complete flow definition.")
        return self


class FlowCreate(BaseModel):
    content: str | None = Field(default=None, max_length=100_000)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2_000)
    domains: list[str] = Field(default_factory=lambda: ["general"])
    status: str = "candidate"
    steps: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def has_content_or_definition(self):
        if not self.content and (not self.name or not self.description or not self.steps):
            raise ValueError("Provide content or a complete flow definition.")
        return self


def _flow_document(body: FlowCreate | FlowUpdate, *, fallback_name: str = "") -> str:
    data = (
        yaml.safe_load(body.content)
        if body.content
        else {
            "name": body.name or fallback_name,
            "description": body.description or "",
            "domains": body.domains,
            "status": body.status,
            "steps": body.steps,
        }
    )
    if not isinstance(data, dict):
        raise FlowParseError("Flow definition must be a YAML mapping")
    steps = data.get("steps") or []
    if any(isinstance(step, dict) and ({"tool", "agent"} & step.keys()) for step in steps):
        raise FlowParseError(
            "Flow steps reference only skills. Move agent and tool choices into the skill execution contract."
        )
    # Dashboard edits are executable changes. They may retire a flow, but they
    # cannot bypass evidence-backed activation by writing status: active.
    data["status"] = "retired" if str(data.get("status") or "").lower() == "retired" else "candidate"
    data.pop("version", None)
    data.pop("activation_fingerprint", None)
    return yaml.safe_dump(
        data,
        sort_keys=False,
        allow_unicode=True,
    )


def _validate_flow_definition(flow) -> None:
    services = current_services()
    report = validate_flow_capabilities(
        flow,
        skill_registry=services.skill_registry,
        agent_registry=services.agent_registry,
        tool_registry=services.tool_registry,
        allow_candidate_skills=flow.status == "candidate",
    )
    if not report.valid:
        raise HTTPException(status_code=422, detail="Flow is not executable: " + "; ".join(report.errors))


def _execution_view(execution) -> dict[str, Any] | None:
    if execution is None:
        return None
    return {
        "agent": execution.agent,
        "tools": list(execution.tools),
        "inputs": execution.inputs,
        "outputs": execution.outputs,
        "approval": execution.approval,
        "success_criteria": list(execution.success_criteria),
        "checks": [{check.name: check.args} if check.args else check.name for check in execution.checks],
    }


def _validate_execution_dependencies(execution, domains: list[str]) -> None:
    if execution is None:
        return
    services = current_services()
    if services.agent_registry is not None:
        try:
            agent = services.agent_registry.get(execution.agent)
        except Exception:
            raise HTTPException(status_code=422, detail=f"Unknown executor {execution.agent!r}") from None
        if agent.domain not in domains:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Executor {execution.agent!r} has domain {agent.domain!r}, which is not "
                    "listed in the skill domains"
                ),
            )
    if services.tool_registry is not None:
        for tool_name in execution.tools:
            if tool_name.startswith("$input."):
                continue
            try:
                services.tool_registry.get(tool_name)
            except Exception:
                raise HTTPException(status_code=422, detail=f"Unknown tool {tool_name!r}") from None


@router.get("/skills")
async def list_skills() -> list[dict[str, Any]]:
    registry = current_services().require("skill_registry")
    return [
        {
            "name": skill.name,
            "description": skill.description,
            "source": skill.source.value,
            "version": skill.version,
            "status": skill.status,
            "domains": sorted(skill.domains),
            "execution": _execution_view(skill.execution),
        }
        for skill in sorted(registry.all(), key=lambda item: item.name)
    ]


@router.post("/skills", status_code=201)
async def create_skill(body: SkillCreate) -> dict[str, Any]:
    registry = current_services().require("skill_registry")
    name = body.name.strip()
    if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
        raise HTTPException(status_code=422, detail="Skill name must use lowercase letters, digits, and hyphens")
    if name in registry.names():
        raise HTTPException(status_code=409, detail=f"Skill {name!r} already exists")
    learned_dir = Path(current_services().require("north_home")) / "skills"
    description = body.description.strip()
    instructions = body.instructions.strip()
    if not description.startswith("Use when"):
        raise HTTPException(status_code=422, detail="Skill description must start with 'Use when'.")
    reason = rejection_reason(name, description, instructions, source=SkillSource.LEARNED)
    if reason:
        raise HTTPException(status_code=422, detail=reason)
    execution = {
        "agent": body.executor.strip(),
        "tools": body.tools,
        "approval": body.approval,
        "inputs": body.inputs,
        "outputs": body.outputs,
        "success_criteria": body.success_criteria,
    }
    try:
        execution_contract = parse_execution_contract(execution)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    _validate_execution_dependencies(execution_contract, body.domains)
    document = (
        "---\n"
        + yaml.safe_dump(
            {
                "name": name,
                "description": description,
                "source": SkillSource.LEARNED.value,
                "version": "1.0.0",
                "status": "candidate",
                "domains": body.domains,
                "execution": execution,
            },
            sort_keys=False,
        )
        + "---\n\n"
        + instructions
        + "\n"
    )
    target = learned_dir / name
    await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread((target / SKILL_FILENAME).write_text, document, encoding="utf-8")
    registry.reload()
    return await get_skill(name)


@router.post("/skills/{name}/duplicate", status_code=201)
async def duplicate_skill(name: str, body: SkillDuplicate) -> dict[str, Any]:
    registry = current_services().require("skill_registry")
    try:
        skill = registry.get(name)
    except SkillNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    new_name = body.name.strip()
    if not re.fullmatch(r"[a-z][a-z0-9-]*", new_name):
        raise HTTPException(status_code=422, detail="Skill name must use lowercase letters, digits, and hyphens")
    if new_name in registry.names():
        raise HTTPException(status_code=409, detail=f"Skill {new_name!r} already exists")
    content = await asyncio.to_thread(
        (skill.directory / SKILL_FILENAME).read_text,
        encoding="utf-8",
    )
    frontmatter, content_body = parse_skill_document(content)
    frontmatter["name"] = new_name
    frontmatter["source"] = SkillSource.LEARNED.value
    frontmatter["status"] = "candidate"
    learned_dir = Path(current_services().require("north_home")) / "skills"
    target = learned_dir / new_name
    document = "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n\n" + content_body.strip() + "\n"
    await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread((target / SKILL_FILENAME).write_text, document, encoding="utf-8")
    registry.reload()
    return await get_skill(new_name)


@router.get("/skills/{name}")
async def get_skill(name: str) -> dict[str, Any]:
    registry = current_services().require("skill_registry")
    try:
        skill = registry.get(name)
    except SkillNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    content = await asyncio.to_thread((skill.directory / "SKILL.md").read_text, encoding="utf-8")
    return {
        "name": skill.name,
        "content": content,
        "source": skill.source.value,
        "execution": _execution_view(skill.execution),
    }


@router.put("/skills/{name}")
async def update_skill(name: str, body: SkillUpdate) -> dict[str, Any]:
    registry = current_services().require("skill_registry")
    try:
        skill = registry.get(name)
    except SkillNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    try:
        frontmatter, content_body = parse_skill_document(body.content)
    except SkillParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    parsed_name = str(frontmatter.get("name") or "").strip()
    description = str(frontmatter.get("description") or "").strip()
    reason = rejection_reason(parsed_name, description, content_body, source=SkillSource.LEARNED)
    if parsed_name != name or reason:
        raise HTTPException(status_code=422, detail=reason or "Skill name cannot be changed")
    status = str(frontmatter.get("status") or "candidate").strip().lower()
    if status not in {"candidate", "active", "retired"}:
        raise HTTPException(status_code=422, detail=f"Invalid skill status: {status!r}")
    try:
        execution = parse_execution_contract(frontmatter.get("execution"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    raw_domains = frontmatter.get("domains") or ["engineering"]
    domains = (
        [str(domain).strip() for domain in raw_domains if str(domain).strip()]
        if isinstance(raw_domains, list)
        else ["engineering"]
    )
    _validate_execution_dependencies(execution, domains)
    # Any instruction edit invalidates selection and execution evidence. The
    # dashboard may retire a skill, but activation goes through create_skill.
    frontmatter["status"] = "retired" if status == "retired" else "candidate"
    frontmatter["source"] = SkillSource.LEARNED.value
    document = "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n\n" + content_body.strip() + "\n"
    target = skill.directory
    if skill.source is SkillSource.BUILTIN:
        target = Path(current_services().require("north_home")) / "skills" / name
        await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread((target / SKILL_FILENAME).write_text, document, encoding="utf-8")
    registry.reload()
    return await get_skill(name)


@router.delete("/skills/{name}", status_code=204)
async def delete_skill(name: str) -> None:
    """Delete a user-owned skill or override and reveal the built-in fallback.

    A skill a flow runs is kept unless a built-in takes its place: the flow
    could only fail the next time it ran.
    """
    registry = current_services().require("skill_registry")
    skill = next((item for item in registry.all() if item.name == name), None)
    stranded = (
        _flows_using(name)
        if skill is not None and skill.source is SkillSource.LEARNED and not registry.overrides_builtin(name)
        else []
    )
    if stranded:
        raise HTTPException(
            status_code=409,
            detail=f"Used by {', '.join(stranded)}. Delete those flows or change their steps first.",
        )
    if skill is None or not registry.remove_learned(name):
        if skill is not None and skill.source.value == "builtin":
            raise HTTPException(status_code=403, detail="Built-in skills cannot be deleted")
        raise HTTPException(status_code=404, detail=f"Learned skill {name!r} was not found")
    _forget_authorship(skill.directory)


def _flows_using(skill: str) -> list[str]:
    registry = current_services().flow_registry
    flows = registry.all() if registry is not None else []
    return sorted(flow.name for flow in flows if any(step.skill == skill for step in flow.steps))


def _forget_authorship(directory: Path) -> None:
    """North no longer owns what it wrote in a deleted *directory*, so what you make there later stays yours."""
    home = Path(current_services().require("north_home"))
    SelfEditPolicy(directory.parent, home / "mutations").forget(directory)


@router.get("/tools")
async def list_tools() -> list[dict[str, Any]]:
    registry = current_services().require("tool_registry")
    learned_dir = Path(current_services().require("north_home")) / "learned" / "tools"
    catalog = _list_tools(learned_dir)
    rows = catalog.data.get("tools", []) if catalog.data else []
    result = {
        tool.name: {
            "name": tool.name,
            "description": tool.description,
            "source": "learned" if str(type(tool).__module__).startswith("north_learned_tool_") else "built-in",
            "status": "active",
            "mutating": tool.is_mutating,
        }
        for tool in registry.available_tools()
    }
    for row in rows:
        if row["name"] not in result and row["status"] != "candidate":
            continue
        try:
            active = registry.get(row["name"])
        except Exception:
            active = None
        result[row["name"]] = {
            "name": row["name"],
            "description": row["description"],
            "source": "learned" if row["source"] == "learned" else "built-in",
            "status": row["status"],
            "mutating": bool(active and active.is_mutating),
        }
    return [result[name] for name in sorted(result)]


@router.post("/tools", status_code=201)
async def create_tool(body: ToolCreate) -> dict[str, Any]:
    registry = current_services().require("tool_registry")
    name = body.name.strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise HTTPException(status_code=422, detail="Tool name must use snake_case")
    if name in registry.all_tool_names():
        raise HTTPException(status_code=409, detail=f"Tool {name!r} already exists")
    learned_dir = Path(current_services().require("north_home")) / "learned" / "tools"
    target = _candidate_root(learned_dir) / body.tool_type / f"{name}.py"
    if target.exists():
        raise HTTPException(status_code=409, detail=f"Tool candidate {name!r} already exists")
    await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
    content = _render_stub(
        name,
        "".join(word.title() for word in name.split("_")) + "Tool",
        body.description.strip(),
        [],
        "Created from the North dashboard.",
    )
    await asyncio.to_thread(target.write_text, content, encoding="utf-8")
    return {
        "name": name,
        "description": body.description.strip(),
        "source": "learned",
        "status": "candidate",
        "mutating": False,
    }


@router.get("/tools/{name}")
async def get_tool(name: str) -> dict[str, Any]:
    registry = current_services().require("tool_registry")
    learned_dir = Path(current_services().require("north_home")) / "learned" / "tools"
    path = _find_tool_path(name, learned_dir)
    if path is None:
        raise HTTPException(status_code=404, detail=f"Tool {name!r} was not found")
    content = await asyncio.to_thread(path.read_text, encoding="utf-8")
    candidate = _find_candidate_path(name, learned_dir)
    try:
        active = registry.get(name)
    except Exception:
        active = None
    catalog = _list_tools(learned_dir)
    row = next((item for item in (catalog.data or {}).get("tools", []) if item["name"] == name), None)
    return {
        "name": name,
        "description": row["description"] if row else (active.description if active else ""),
        "source": "learned" if path.is_relative_to(learned_dir) else "built-in",
        "status": "candidate" if candidate is not None else "active",
        "content": content,
        "mutating": bool(active and active.is_mutating),
    }


@router.put("/tools/{name}")
async def update_tool(name: str, body: ToolUpdate) -> dict[str, Any]:
    learned_dir = Path(current_services().require("north_home")) / "learned" / "tools"
    path = _find_tool_path(name, learned_dir)
    if path is None:
        raise HTTPException(status_code=404, detail=f"Tool {name!r} was not found")
    kind = path.parent.name if path.parent.name in {"universal", "specialized"} else "specialized"
    target = _candidate_root(learned_dir) / kind / f"{name}.py"
    safe, reason = _check_code_safety(body.content)
    if not safe:
        raise HTTPException(status_code=422, detail=f"Tool code rejected by safety check: {reason}")
    if f'name = "{name}"' not in body.content and f"name = '{name}'" not in body.content:
        raise HTTPException(status_code=422, detail=f"Tool code must keep name = {name!r}")
    await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(target.write_text, body.content, encoding="utf-8")
    return await get_tool(name)


@router.delete("/tools/{name}", status_code=204)
async def delete_tool(name: str) -> None:
    registry = current_services().require("tool_registry")
    learned_dir = Path(current_services().require("north_home")) / "learned" / "tools"
    candidate = _find_candidate_path(name, learned_dir)
    if candidate is not None:
        await asyncio.to_thread(candidate.unlink)
        return
    path = _find_tool_path(name, learned_dir)
    if path is None or not path.is_relative_to(learned_dir):
        raise HTTPException(status_code=403, detail="Built-in tools cannot be deleted")
    await asyncio.to_thread(path.unlink)
    registry.remove(name)


@router.get("/flow-definitions")
async def list_flows() -> list[dict[str, Any]]:
    registry = current_services().require("flow_registry")
    return [
        {
            "name": flow.name,
            "description": flow.description,
            "source": flow.source.value,
            "status": flow.status,
            "domains": sorted(flow.domains),
            "steps": len(flow.steps),
        }
        for flow in sorted(registry.all(), key=lambda item: item.name)
    ]


def _changed_since_activation(flow) -> bool:
    """An active flow whose definition or skills changed after it was activated.

    The scheduler refuses to run it until it is tested and activated again, the same check
    `FlowRunner.run` makes. Its status still says "active", which hid the Activate button.
    """
    if flow.status != "active" or not flow.activation_fingerprint:
        return False
    skills = current_services().skill_registry
    return flow.activation_fingerprint != flow_fingerprint(flow, skills.get if skills is not None else None)


def _tested_run_id(flow) -> str:
    """The latest completed test-mode run of this exact flow definition, when it needs activating, or ""."""
    services = current_services()
    store = services.flow_store
    if store is None or not (flow.status == "candidate" or _changed_since_activation(flow)):
        return ""
    skills = services.skill_registry
    fingerprint = flow_fingerprint(flow, skills.get if skills is not None else None)
    for run in store.list_runs(flow.name, 50):
        if run.test_mode and run.status == "completed" and run.flow_fingerprint == fingerprint:
            return run.run_id
    return ""


_RUN_TEXT_LIMIT = 1_200


def _clip(value: Any, limit: int = _RUN_TEXT_LIMIT) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _meaningful(value: Any) -> str:
    text = _clip(value)
    return "" if text in {"{}", "[]"} else text


def stopped_step_view(run: FlowRun, flow: Flow | None, waiting: Card | None = None) -> dict[str, Any] | None:
    """The step an unfinished run is on, what runs it, and the card it waits on, if any.

    A run's details listed only finished steps, so one waiting on an approval, or
    paused on an error before its first step finished, said "No step finished"
    and nothing about where it was or why.
    """
    if run.status == "completed" or flow is None or not 0 <= run.current_step < len(flow.steps):
        return None
    step = flow.steps[run.current_step]
    return {
        "step": step.name,
        "runs": step.skill or step.action or "instructions",
        "waiting_for": waiting.title if waiting is not None else "",
        "waiting_since": waiting.created_at.isoformat() if waiting is not None else "",
    }


def flow_run_view(
    run: FlowRun, total_steps: int | None = None, *, stopped_at: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One run as the Flows page reads it: what started it, how far it got, what it said.

    Step output is clipped: a run keeps whole agent answers, and a history list
    needs enough to recognise the run, not a copy of every briefing.
    """
    steps = []
    for item in run.outputs:
        data = item.get("data") or {}
        steps.append(
            {
                "step": item.get("step", ""),
                "skill": item.get("skill", ""),
                "agent": item.get("agent", ""),
                # A step whose skill declares no outputs answers with an empty
                # object, which says nothing worth showing.
                "summary": _meaningful(data.get("summary")),
                "output": _meaningful(data.get("output")),
                "tools_used": [str(tool) for tool in data.get("tools_used") or []],
                # What the step left behind, named the way the Artifacts page names it.
                "artifacts": [
                    {"name": Path(path).name, "kind": Path(path).parent.name} for path in data.get("artifacts") or []
                ],
            }
        )
    return {
        "run_id": run.run_id,
        "flow": run.flow_name,
        "status": run.status,
        # Runs recorded before the trigger was kept are only known to be tests or not.
        "trigger": run.trigger or ("test" if run.test_mode else ""),
        "test_mode": run.test_mode,
        "task_id": run.task_id,
        "current_step": run.current_step,
        "total_steps": total_steps if total_steps is not None else len(steps),
        "error": run.error,
        "started_at": run.created_at or run.updated_at,
        "updated_at": run.updated_at,
        "steps": steps,
        "stopped_at": stopped_at,
    }


@router.get("/flow-runs")
async def list_flow_runs(flow: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Recent flow runs, newest first; pass `flow` for one flow's history."""
    services = current_services()
    store = services.require("flow_store")
    registry = services.flow_registry
    runs = await asyncio.to_thread(store.list_runs, flow, max(1, min(limit, 200)))
    approvals = services.approval_store
    # Newest first, so each task keeps its oldest open card: the one it is stuck on.
    pending = sorted(approvals.pending(), key=lambda card: card.created_at, reverse=True) if approvals else []
    waiting = {card.task_id: card for card in pending}

    def definition(name: str) -> Flow | None:
        if registry is None:
            return None
        try:
            return registry.get(name)
        except FlowNotFoundError:
            return None

    views = []
    for run in runs:
        known = definition(run.flow_name)
        views.append(
            flow_run_view(
                run,
                len(known.steps) if known is not None else None,
                stopped_at=stopped_step_view(run, known, waiting.get(run.task_id)),
            )
        )
    # A scheduled run's task id is its job's id; the job knows the slot it was due
    # at, so a run hours late says so instead of reading as on time.
    if any(view["trigger"] == "schedule" for view in views) and services.job_processor is not None:
        jobs = {job.job_id: job for job in await services.job_processor.list_jobs(limit=200)}
        for view in views:
            job = jobs.get(view["task_id"]) if view["trigger"] == "schedule" else None
            view["late_note"] = late_note(job) if job is not None else ""
    return views


class FlowRunRequest(BaseModel):
    mode: str = Field(default="test", pattern="^(test|execute)$")
    inputs: dict[str, Any] = Field(default_factory=dict)


class FlowActivation(BaseModel):
    test_run_id: str = Field(min_length=1, max_length=100)


class FlowScheduleCreate(BaseModel):
    """When a flow should run. The flow carries the work, so there is no prompt or agent."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(default="", max_length=200)
    hour: int | None = None
    minute: int | None = None
    interval_minutes: int | None = None
    days: Any = None
    run_at: str | None = None


class FlowScheduleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, max_length=200)
    hour: int | None = None
    minute: int | None = None
    interval_minutes: int | None = None
    days: Any = None
    enabled: bool | None = None


async def _remove_schedules_of(flow: str) -> None:
    """A schedule with no flow can only fail when it fires, so it goes with the flow."""
    services = current_services()
    if services.cron_store is not None:
        for row in await services.cron_store.list():
            if row.get("flow") == flow:
                await services.cron_store.remove(row["name"])
    if services.job_processor is not None:
        for job in await services.job_processor.list_jobs(status=JobStatus.PENDING, limit=1000):
            if (job.payload or {}).get("flow") == flow:
                await services.job_processor.cancel(job.job_id)


def _known_flow(name: str):
    try:
        return current_services().require("flow_registry").get(name)
    except FlowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


async def _as_user(tool: Tool, params: dict[str, Any]) -> ToolOutput:
    """Run *tool* for the user, who is acting from the dashboard: there is nobody else to ask.

    Every other caller goes through `Tool.execute`, which puts the call to the
    approval layer first.
    """
    return await tool.run(ToolInput(params=params))


def _set_fields(model: BaseModel) -> dict[str, Any]:
    return {key: value for key, value in model.model_dump().items() if value is not None}


async def _flow_schedule_row(name: str) -> dict[str, Any]:
    """The stored schedule *name*, if it is one that runs a flow."""
    row = await current_services().require("cron_store").get(name)
    if row is None or not row.get("flow"):
        raise HTTPException(status_code=404, detail=f"No flow schedule named {name!r}")
    return row


def _schedule_tools() -> tuple[ScheduleTaskTool, UpdateScheduleTool, CancelScheduleTool]:
    services = current_services()
    common = {
        "skill_registry": services.skill_registry,
        "flow_registry": services.flow_registry,
        "agent_registry": services.agent_registry,
        "tool_registry": services.tool_registry,
    }
    cron_store = services.require("cron_store")
    return (
        ScheduleTaskTool(job_processor=services.require("job_processor"), cron_store=cron_store, **common),
        UpdateScheduleTool(cron_store=cron_store, **common),
        CancelScheduleTool(job_processor=services.require("job_processor"), cron_store=cron_store),
    )


@router.post("/flow-definitions/{name}/schedules", status_code=201)
async def create_flow_schedule(name: str, body: FlowScheduleCreate) -> dict[str, Any]:
    """Put a flow on a schedule: a repeating one, or a single run at a set time.

    Goes through the same tool the chat uses, so a schedule made here obeys the
    same rules: the flow must be active, unchanged since it was tested, and
    still executable.
    """
    flow = _known_flow(name)
    scheduler, _, _ = _schedule_tools()
    result = await _as_user(
        scheduler,
        {"task": flow.description or f"Run the {name} flow.", "agent": "general", "flow": name, **_set_fields(body)},
    )
    if not result.success:
        raise HTTPException(status_code=422, detail=result.error or "Could not schedule this flow")
    return result.data


@router.patch("/flow-schedules/{schedule}")
async def update_flow_schedule(schedule: str, body: FlowScheduleUpdate) -> dict[str, Any]:
    """Retime, rename, pause or resume one of a flow's schedules."""
    await _flow_schedule_row(schedule)
    _, updater, _ = _schedule_tools()
    result = await _as_user(updater, {"name": schedule, **_set_fields(body)})
    if not result.success:
        raise HTTPException(status_code=422, detail=result.error or "Could not change this schedule")
    return result.data


@router.delete("/flow-schedules/{schedule}", status_code=204)
async def delete_flow_schedule(schedule: str) -> None:
    await _flow_schedule_row(schedule)
    _, _, canceller = _schedule_tools()
    result = await _as_user(canceller, {"name": schedule})
    if not result.success:
        raise HTTPException(status_code=409, detail=result.error or "Could not delete this schedule")


@router.post("/flow-definitions/{name}/runs", status_code=202)
async def start_flow_run(name: str, body: FlowRunRequest) -> dict[str, Any]:
    """Start a run in the background and return at once; it can take minutes.

    ``test`` is how a candidate earns activation. ``execute`` is a real run and
    needs the flow to be schedulable: active, unchanged since it was tested.
    Approvals the run needs appear in the Approvals page like any other.
    """
    services = current_services()
    flow = _known_flow(name)
    runner = services.require("flow_runner")
    store = services.require("flow_store")
    skills = services.skill_registry
    test_mode = body.mode == "test"
    if test_mode:
        if flow.status not in {"candidate", "active"}:
            raise HTTPException(status_code=409, detail=f"Flow '{name}' is {flow.status}, so it cannot be run.")
        report = validate_flow_capabilities(
            flow,
            skill_registry=skills,
            agent_registry=services.agent_registry,
            tool_registry=services.tool_registry,
            allow_candidate_skills=True,
        )
        problem = "Flow is not executable: " + "; ".join(report.errors) if not report.valid else ""
    else:
        problem = schedulable_flow_error(
            flow,
            skill_registry=skills,
            agent_registry=services.agent_registry,
            tool_registry=services.tool_registry,
        )
    if problem:
        raise HTTPException(status_code=422, detail=problem)

    run_id = generate_id()
    task_id = f"flow_{run_id[:12]}"
    trigger = "test" if test_mode else "manual"
    # Created before returning so the run is on the page the moment it starts.
    store.create(
        run_id=run_id,
        flow_name=name,
        task_id=task_id,
        agent="general",
        flow_fingerprint=flow_fingerprint(flow, skills.get if skills is not None else None),
        test_mode=test_mode,
        trigger=trigger,
        inputs=body.inputs,
    )

    _drive_in_background(
        runner, store, name, run_id=run_id, task_id=task_id, test_mode=test_mode, trigger=trigger, inputs=body.inputs
    )
    return {"run_id": run_id, "task_id": task_id, "status": "running", "trigger": trigger}


def _drive_in_background(runner, store, name: str, **run: Any) -> None:
    """Run a flow without holding the request; an unexpected error pauses the run where it was."""
    run_id = run["run_id"]

    async def _drive() -> None:
        try:
            await runner.run(name, **run)
        except Exception as exc:
            current = store.get(run_id)
            if current is not None and current.status == "running":
                store.update(
                    run_id,
                    status="paused",
                    current_step=current.current_step,
                    outputs=current.outputs,
                    error=str(exc),
                )
            raise

    spawn(_drive(), name=f"flow_run:{run_id}")


@router.post("/flow-runs/{run_id}/resume", status_code=202)
async def resume_flow_run(run_id: str) -> dict[str, Any]:
    """Run a paused run again from the step it stopped on (#33).

    Finished steps are not repeated, and the approval layer refuses anything
    that already reached someone or spent money in this run. A run whose flow
    or skills changed since it started cannot resume: what it would run is not
    what it ran, so it is discarded and started again instead.
    """
    services = current_services()
    runner = services.require("flow_runner")
    store = services.require("flow_store")
    run = store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No flow run with that id")
    if run.status not in ("paused", "waiting"):
        raise HTTPException(
            status_code=409, detail=f"Only a paused or waiting run can be resumed; this one is {run.status}."
        )
    flow = _known_flow(run.flow_name)
    skills = services.skill_registry
    if run.flow_fingerprint and run.flow_fingerprint != flow_fingerprint(flow, skills.get if skills else None):
        raise HTTPException(
            status_code=409,
            detail=f"'{run.flow_name}' or one of its skills changed since this run started. "
            "Discard it and start a new run.",
        )
    _drive_in_background(
        runner,
        store,
        run.flow_name,
        run_id=run.run_id,
        task_id=run.task_id,
        test_mode=run.test_mode,
        trigger=run.trigger,
    )
    return {"run_id": run.run_id, "status": "running", "resumed_at_step": run.current_step}


@router.post("/flow-runs/{run_id}/discard")
async def discard_flow_run(run_id: str) -> dict[str, Any]:
    """End a paused run without finishing it; the steps it finished stay on record, and its stop card closes."""
    runner = current_services().require("flow_runner")
    try:
        run = runner.discard(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="No flow run with that id") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return flow_run_view(run)


@router.post("/flow-definitions/{name}/activate")
async def activate_flow(name: str, body: FlowActivation) -> dict[str, Any]:
    """Activate a candidate on the evidence of a completed test run.

    Clicking activate in the page is the explicit confirmation the tool asks
    for; the evidence rules are the tool's own, so the chat and the page cannot
    disagree about what earns activation.
    """
    services = current_services()
    flow = _known_flow(name)
    if flow.source is FlowSource.BUILTIN:
        raise HTTPException(status_code=409, detail="A built-in flow is already active as shipped.")
    # Built here rather than taken from the tool registry: the registered tool
    # carries the self-edit policy that guards north editing its own files, and
    # that policy only knows files north wrote itself. This is the user editing
    # through the page, like every other flow write in this module.
    tool = CreateFlowTool(
        services.require("flow_registry"),
        learned_dir=Path(services.require("north_home")) / "flows",
        tool_registry=services.tool_registry,
        skill_registry=services.skill_registry,
        agent_registry=services.agent_registry,
        flow_store=services.flow_store,
    )
    result = await _as_user(tool, {"action": "activate", "name": name, "test_run_id": body.test_run_id})
    if not result.success:
        raise HTTPException(status_code=422, detail=result.error or "Could not activate this flow")
    return await get_flow(name)


@router.get("/flow-definitions/{name}")
async def get_flow(name: str) -> dict[str, Any]:
    registry = current_services().require("flow_registry")
    try:
        flow = registry.get(name)
    except FlowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    content = await asyncio.to_thread((flow.directory / FLOW_FILENAME).read_text, encoding="utf-8")
    return {
        "name": flow.name,
        "description": flow.description,
        "content": content,
        "source": flow.source.value,
        "status": flow.status,
        # The test run that proves this exact candidate works, when there is one:
        # activation is offered only against it.
        "tested_run_id": await asyncio.to_thread(_tested_run_id, flow),
        "changed_since_activation": await asyncio.to_thread(_changed_since_activation, flow),
        "domains": sorted(flow.domains),
        "steps": [
            {
                "name": step.name,
                "skill": step.skill,
                "instructions": step.instructions,
                "inputs": step.inputs,
                "approval": step.approval,
                "action": step.action,
            }
            for step in flow.steps
        ],
    }


@router.post("/flow-definitions", status_code=201)
async def create_flow(body: FlowCreate) -> dict[str, Any]:
    registry = current_services().require("flow_registry")
    learned_dir = Path(current_services().require("north_home")) / "flows"
    try:
        parsed = parse_flow_document(_flow_document(body), learned_dir / "new-flow", FlowSource.LEARNED)
    except (FlowParseError, yaml.YAMLError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    _validate_flow_definition(parsed)
    target = learned_dir / parsed.name
    if target.exists() and parsed.name in registry.names():
        raise HTTPException(status_code=409, detail=f"Flow {parsed.name!r} already exists")
    await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)
    document = _flow_document(body, fallback_name=parsed.name)
    await asyncio.to_thread((target / FLOW_FILENAME).write_text, document, encoding="utf-8")
    registry.reload()
    return await get_flow(parsed.name)


@router.put("/flow-definitions/{name}")
async def update_flow(name: str, body: FlowUpdate) -> dict[str, Any]:
    registry = current_services().require("flow_registry")
    try:
        flow = registry.get(name)
    except FlowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    target = flow.directory
    if flow.source is FlowSource.BUILTIN:
        target = Path(current_services().require("north_home")) / "flows" / name
    try:
        parsed = parse_flow_document(_flow_document(body, fallback_name=name), target, FlowSource.LEARNED)
    except (FlowParseError, yaml.YAMLError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    _validate_flow_definition(parsed)
    if parsed.name != name:
        raise HTTPException(status_code=422, detail="Flow name cannot be changed")
    await asyncio.to_thread(
        target.mkdir,
        parents=True,
        exist_ok=True,
    )
    await asyncio.to_thread(
        (target / FLOW_FILENAME).write_text,
        _flow_document(body, fallback_name=name),
        encoding="utf-8",
    )
    registry.reload()
    return await get_flow(name)


@router.delete("/flow-definitions/{name}", status_code=204)
async def delete_flow(name: str) -> None:
    """Delete a flow with what exists only for it.

    That is its schedules and queued runs, its runs and the cards their stops
    left, the approvals north remembers from its step cards, and North's record
    of having written it. A run still going is refused rather than cut off.
    """
    services = current_services()
    registry = services.require("flow_registry")
    try:
        flow = registry.get(name)
    except FlowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    if flow.source is FlowSource.BUILTIN:
        raise HTTPException(status_code=403, detail="Built-in flows cannot be deleted")
    runs = services.flow_store.list_runs(name, limit=None) if services.flow_store is not None else []
    if any(run.status == "running" for run in runs):
        raise HTTPException(
            status_code=409,
            detail="A run of this flow is still going. Let it finish, or answer the card it waits on, then delete.",
        )
    if not registry.remove_learned(name):
        raise HTTPException(status_code=404, detail=f"Learned flow {name!r} was not found")
    await _remove_schedules_of(name)
    if runs:
        runner = services.require("flow_runner")
        for run in runs:
            if run.status in STOPPED:
                runner.discard(run.run_id)  # closes the card its stop left
        services.flow_store.remove_flow(name)
    if services.approval_memory is not None:
        services.approval_memory.forget_cards_titled(step_approval_title(name))
    _forget_authorship(flow.directory)


def _settings():
    return current_services().require("north_settings")


@router.get("/setup")
async def get_setup():
    settings = _settings()
    return {
        **settings.setup_progress,
        "timezone": settings.timezone,
        "timezone_configured": settings.timezone_configured,
        "profiles": [profile.catalog_entry() for profile in settings.browser_profiles],
        "providers": [
            {
                "id": p.id,
                "name": p.display_name,
                "configured": p.is_configured(settings),
                "optional": not p.env_key and p.auth_kind.value != "oauth_pkce",
            }
            for p in PROVIDER_DEFINITIONS
        ],
    }


@router.get("/setup/coding-agents")
async def coding_agent_status():
    """Installed, recent enough and logged in, per agent, using the agents' own probes.

    Each probe starts the agent's CLI, so this runs when the person asks rather
    than on the setup page's poll.
    """
    from coding_agents.discovery import check_agents

    return [asdict(agent) for agent in await check_agents()]


@router.post("/setup")
async def update_setup(body: SetupProgress):
    _settings().set_setup_progress(body.model_dump())
    return await get_setup()


class ProfilesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profiles: list[BrowserProfile]


@router.get("/browser/profiles")
async def get_browser_profiles():
    runtime = current_services().web_runtime
    return {
        "profiles": [profile.model_dump() for profile in _settings().browser_profiles],
        "tests": runtime.browser_tests if runtime is not None else {},
    }


@router.post("/browser/profiles")
async def update_browser_profiles(body: ProfilesUpdate):
    try:
        _settings().set_browser_profiles(body.profiles)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    runtime = current_services().web_runtime
    if runtime is not None:
        runtime.browser_tests.clear()  # edits invalidate old readiness evidence
    return await get_browser_profiles()


@router.post("/browser/profiles/discover")
async def discover_profiles():
    return {"profiles": await asyncio.to_thread(discover_browser_profiles)}


@router.post("/browser/profiles/{profile_id}/connect")
async def connect_browser_profile(profile_id: str):
    """Prepare the one-time setup for an existing profile: a folder to load in Chrome.

    Writing a folder and a short-lived pairing code touches no browser data; the
    person loads it into the profile themselves, which is the only access grant.
    """
    profile = next((p for p in _settings().browser_profiles if p.id == profile_id), None)
    if profile is None or profile.context != "existing":
        raise HTTPException(status_code=404, detail="Existing browser profile not found")
    tool = current_services().require("tool_registry").get("browser")
    bridge = getattr(tool, "existing_browser", None)
    if bridge is None:
        raise HTTPException(status_code=503, detail="The existing-profile bridge is not available")
    try:
        return await bridge.setup(profile)
    except (RuntimeError, OSError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@router.post("/browser/profiles/{profile_id}/test")
async def test_browser_profile(profile_id: str):
    """Run only the existing preflight tool, with its ordinary central approval.

    This is a bounded diagnostic, not an LLM task with arbitrary tools. Return
    immediately so the dashboard can still answer any pending approval card.
    """
    settings = _settings()
    if not any(profile.id == profile_id and profile.enabled for profile in settings.browser_profiles):
        raise HTTPException(status_code=404, detail="Enabled browser profile not found")
    runtime = current_services().require("web_runtime")
    previous = runtime.browser_tests.get(profile_id, {})
    if previous.get("status") == "running":
        return previous
    tool = current_services().require("tool_registry").get("browser")
    test_id = f"browser-profile-test-{generate_id()}"
    result = {"test_id": test_id, "status": "running", "profile_id": profile_id}
    runtime.browser_tests[profile_id] = result

    async def check():
        try:
            output = await tool.execute(
                ToolInput(params={"action": "preflight", "profile_id": profile_id, "task_id": test_id})
            )
            result.update(status="completed" if output.success else "failed", data=output.data, error=output.error)
        except Exception as exc:
            result.update(status="failed", error=str(exc))

    spawn(check(), name=test_id)
    return result
