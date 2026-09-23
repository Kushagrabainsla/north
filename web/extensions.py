"""Dashboard management routes for agents' procedural skills and flows."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from flows.exceptions import FlowNotFoundError, FlowParseError
from flows.models import FLOW_FILENAME, FlowSource
from flows.registry import parse_flow_document
from orchestrator.api_context import current_services
from skills.exceptions import SkillNotFoundError, SkillParseError
from skills.models import SKILL_FILENAME, SkillSource
from skills.parser import parse_skill_document
from skills.registry import parse_execution_contract, rejection_reason
from tools.universal._flow_validation import validate_flow_capabilities
from tools.universal.create_tool import (
    _candidate_root,
    _check_code_safety,
    _find_candidate_path,
    _find_tool_path,
    _list_tools,
    _render_stub,
)

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
    approval: str = Field(default="never", pattern="^(never|on_mutation|always)$")
    inputs: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    outputs: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
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
    document = "---\n" + yaml.safe_dump(
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
    ) + "---\n\n" + instructions + "\n"
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
    """Delete a user-owned skill or override and reveal the built-in fallback."""
    registry = current_services().require("skill_registry")
    if not registry.remove_learned(name):
        skill = next((item for item in registry.all() if item.name == name), None)
        if skill is not None and skill.source.value == "builtin":
            raise HTTPException(status_code=403, detail="Built-in skills cannot be deleted")
        raise HTTPException(status_code=404, detail=f"Learned skill {name!r} was not found")


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
        "domains": sorted(flow.domains),
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
    registry = current_services().require("flow_registry")
    try:
        flow = registry.get(name)
    except FlowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    if flow.source is FlowSource.BUILTIN:
        raise HTTPException(status_code=403, detail="Built-in flows cannot be deleted")
    if not registry.remove_learned(name):
        raise HTTPException(status_code=404, detail=f"Learned flow {name!r} was not found")
