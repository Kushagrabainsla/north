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
from skills.registry import rejection_reason
from tools.universal.create_tool import _check_code_safety, _find_tool_path, _render_stub

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
    version: str = "1.0.0"
    domains: list[str] = Field(default_factory=lambda: ["general"])
    status: str = "active"
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
    version: str = "1.0.0"
    domains: list[str] = Field(default_factory=lambda: ["general"])
    status: str = "active"
    steps: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def has_content_or_definition(self):
        if not self.content and (not self.name or not self.description or not self.steps):
            raise ValueError("Provide content or a complete flow definition.")
        return self


def _flow_document(body: FlowCreate | FlowUpdate, *, fallback_name: str = "") -> str:
    if body.content:
        return body.content
    import yaml

    return yaml.safe_dump(
        {
            "name": body.name or fallback_name,
            "description": body.description or "",
            "version": body.version,
            "domains": body.domains,
            "status": body.status,
            "steps": body.steps,
        },
        sort_keys=False,
        allow_unicode=True,
    )


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
    document = "---\n" + yaml.safe_dump(
        {
            "name": name,
            "description": body.description.strip(),
            "version": "1.0.0",
            "status": "active",
            "domains": body.domains,
        },
        sort_keys=False,
    ) + "---\n\n" + body.instructions.strip() + "\n"
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
    frontmatter, content_body = parse_skill_document(await asyncio.to_thread((skill.directory / SKILL_FILENAME).read_text, encoding="utf-8"))
    frontmatter["name"] = new_name
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
    return {"name": skill.name, "content": content, "source": skill.source.value}


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
    status = str(frontmatter.get("status") or "active").strip().lower()
    if status not in {"candidate", "active", "retired"}:
        raise HTTPException(status_code=422, detail=f"Invalid skill status: {status!r}")
    target = skill.directory
    if skill.source is SkillSource.BUILTIN:
        target = Path(current_services().require("north_home")) / "skills" / name
        await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread((target / SKILL_FILENAME).write_text, body.content, encoding="utf-8")
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
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "source": (
                "learned"
                if str(getattr(tool, "__module__", "")).startswith("north_learned_tool_")
                else "built-in"
            ),
            "status": "active",
            "mutating": tool.is_mutating,
        }
        for tool in sorted(registry.available_tools(), key=lambda item: item.name)
    ]


@router.post("/tools", status_code=201)
async def create_tool(body: ToolCreate) -> dict[str, Any]:
    registry = current_services().require("tool_registry")
    name = body.name.strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise HTTPException(status_code=422, detail="Tool name must use snake_case")
    if name in registry.all_tool_names():
        raise HTTPException(status_code=409, detail=f"Tool {name!r} already exists")
    learned_dir = Path(current_services().require("north_home")) / "learned" / "tools"
    target = learned_dir / body.tool_type / f"{name}.py"
    await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
    content = _render_stub(
        name,
        "".join(word.title() for word in name.split("_")) + "Tool",
        body.description.strip(),
        [],
        "Created from the North dashboard.",
    )
    await asyncio.to_thread(target.write_text, content, encoding="utf-8")
    registry.reload()
    return {
        "name": name,
        "description": body.description.strip(),
        "source": "learned",
        "status": "active",
        "mutating": False,
    }


@router.get("/tools/{name}")
async def get_tool(name: str) -> dict[str, Any]:
    registry = current_services().require("tool_registry")
    try:
        tool = registry.get(name)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Tool {name!r} was not found") from exc
    learned_dir = Path(current_services().require("north_home")) / "learned" / "tools"
    path = _find_tool_path(name, learned_dir)
    content = await asyncio.to_thread(path.read_text, encoding="utf-8") if path else ""
    source = "learned" if path and path.is_relative_to(learned_dir) else "built-in"
    return {
        "name": tool.name,
        "description": tool.description,
        "source": source,
        "content": content,
        "mutating": tool.is_mutating,
    }


@router.put("/tools/{name}")
async def update_tool(name: str, body: ToolUpdate) -> dict[str, Any]:
    registry = current_services().require("tool_registry")
    learned_dir = Path(current_services().require("north_home")) / "learned" / "tools"
    path = _find_tool_path(name, learned_dir)
    if path is None:
        raise HTTPException(status_code=404, detail=f"Tool {name!r} was not found")
    target = path
    if not path.is_relative_to(learned_dir):
        kind = path.parent.name if path.parent.name in {"universal", "specialized"} else "specialized"
        target = learned_dir / kind / f"{name}.py"
    safe, reason = _check_code_safety(body.content)
    if not safe:
        raise HTTPException(status_code=422, detail=f"Tool code rejected by safety check: {reason}")
    await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(target.write_text, body.content, encoding="utf-8")
    registry.reload()
    return await get_tool(name)


@router.delete("/tools/{name}", status_code=204)
async def delete_tool(name: str) -> None:
    registry = current_services().require("tool_registry")
    learned_dir = Path(current_services().require("north_home")) / "learned" / "tools"
    path = _find_tool_path(name, learned_dir)
    if path is None or not path.is_relative_to(learned_dir):
        raise HTTPException(status_code=403, detail="Built-in tools cannot be deleted")
    await asyncio.to_thread(path.unlink)
    registry.reload()


@router.get("/flow-definitions")
async def list_flows() -> list[dict[str, Any]]:
    registry = current_services().require("flow_registry")
    return [
        {
            "name": flow.name,
            "description": flow.description,
            "source": flow.source.value,
            "version": flow.version,
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
        "version": flow.version,
        "status": flow.status,
        "domains": sorted(flow.domains),
        "steps": [
            {
                "name": step.name,
                "tool": step.tool,
                "params": step.params,
                "skill": step.skill,
                "approval": step.approval,
                "description": step.description,
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
    except FlowParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
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
    except FlowParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
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
