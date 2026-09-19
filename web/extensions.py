"""Dashboard management routes for agents' procedural skills and flows."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from flows.exceptions import FlowNotFoundError, FlowParseError
from flows.models import FLOW_FILENAME, FlowSource
from flows.registry import parse_flow_document
from orchestrator.api_context import current_services
from skills.exceptions import SkillNotFoundError, SkillParseError
from skills.parser import parse_skill_document
from skills.registry import rejection_reason

# This router is composed into ``web.api.router``, which owns the public
# ``/web/api`` prefix and request-level dependencies. Repeating either here
# silently mounts these endpoints at ``/web/api/web/api/...``.
router = APIRouter(tags=["web"])


class SkillUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)


class FlowUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)


class FlowCreate(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)


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
    if skill.source.value == "builtin":
        raise HTTPException(status_code=403, detail="Built-in skills cannot be edited")
    try:
        frontmatter, content_body = parse_skill_document(body.content)
    except SkillParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    parsed_name = str(frontmatter.get("name") or "").strip()
    description = str(frontmatter.get("description") or "").strip()
    reason = rejection_reason(parsed_name, description, content_body, source=skill.source)
    if parsed_name != name or reason:
        raise HTTPException(status_code=422, detail=reason or "Skill name cannot be changed")
    status = str(frontmatter.get("status") or "active").strip().lower()
    if status not in {"candidate", "active", "retired"}:
        raise HTTPException(status_code=422, detail=f"Invalid skill status: {status!r}")
    await asyncio.to_thread((skill.directory / "SKILL.md").write_text, body.content, encoding="utf-8")
    registry.reload()
    return await get_skill(name)


@router.delete("/skills/{name}", status_code=204)
async def delete_skill(name: str) -> None:
    """Delete a learned skill; bundled skills are immutable."""
    registry = current_services().require("skill_registry")
    if not registry.remove_learned(name):
        skill = next((item for item in registry.all() if item.name == name), None)
        if skill is not None and skill.source.value == "builtin":
            raise HTTPException(status_code=403, detail="Built-in skills cannot be deleted")
        raise HTTPException(status_code=404, detail=f"Learned skill {name!r} was not found")


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
        "content": content,
        "source": flow.source.value,
        "steps": [
            {
                "name": step.name,
                "tool": step.tool,
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
        parsed = parse_flow_document(body.content, learned_dir / "new-flow", FlowSource.LEARNED)
    except FlowParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    target = learned_dir / parsed.name
    if target.exists() and parsed.name in registry.names():
        raise HTTPException(status_code=409, detail=f"Flow {parsed.name!r} already exists")
    await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread((target / FLOW_FILENAME).write_text, body.content, encoding="utf-8")
    registry.reload()
    return await get_flow(parsed.name)


@router.put("/flow-definitions/{name}")
async def update_flow(name: str, body: FlowUpdate) -> dict[str, Any]:
    registry = current_services().require("flow_registry")
    try:
        flow = registry.get(name)
    except FlowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    if flow.source is FlowSource.BUILTIN:
        raise HTTPException(status_code=403, detail="Built-in flows cannot be edited")
    try:
        parsed = parse_flow_document(body.content, flow.directory, FlowSource.LEARNED)
    except FlowParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if parsed.name != name:
        raise HTTPException(status_code=422, detail="Flow name cannot be changed")
    await asyncio.to_thread((flow.directory / FLOW_FILENAME).write_text, body.content, encoding="utf-8")
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
