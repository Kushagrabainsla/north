"""The dashboard's Secrets panel: your keys and passwords for north's tools (CODING_STYLE §7.6).

You add, replace and remove them here, and only here: no tool can. No route returns
a value, not even part of one; the panel lists names, notes, when each was set, and
which tools say they use it. What you do here is yours, so it asks nothing
(CODING_STYLE §7.3).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from config.secret_store import MAX_VALUE_LENGTH, SecretStore, unusable
from orchestrator.api_context import current_services

logger = logging.getLogger(__name__)
router = APIRouter(tags=["web"])


class SecretUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=MAX_VALUE_LENGTH)
    note: str = Field("", max_length=200)


def _store() -> SecretStore:
    return SecretStore(current_services().require("north_home"))


def _used_by() -> dict[str, list[str]]:
    """Which tools declare each secret, by secret name."""
    registry = current_services().tool_registry
    users: dict[str, list[str]] = {}
    for tool in registry.all_tools() if registry is not None else []:
        for name in tool.uses_secrets:
            users.setdefault(name, []).append(tool.name)
    return {name: sorted(tools) for name, tools in users.items()}


@router.get("/secrets")
async def list_secrets() -> dict[str, Any]:
    saved = await asyncio.to_thread(_store().saved)
    used_by = _used_by()
    names = {item.name for item in saved}
    return {
        "secrets": [
            {"name": item.name, "note": item.note, "updated_at": item.updated_at, "used_by": used_by.get(item.name, [])}
            for item in saved
        ],
        # A tool can declare a secret before you saved it: the panel says which are still missing.
        "missing": [{"name": name, "used_by": tools} for name, tools in sorted(used_by.items()) if name not in names],
    }


@router.put("/secrets/{name}")
async def save_secret(name: str, body: SecretUpdate) -> dict[str, Any]:
    if why := unusable(name, body.value):
        raise HTTPException(status_code=400, detail=why)
    await asyncio.to_thread(_store().save, name, body.value, body.note)
    logger.info("Secrets: %s saved from the dashboard", name)  # the name only, never the value
    return {"name": name, "saved": True}


@router.delete("/secrets/{name}")
async def remove_secret(name: str) -> dict[str, Any]:
    if not await asyncio.to_thread(_store().remove, name):
        raise HTTPException(status_code=404, detail=f"There is no secret named {name!r}.")
    logger.info("Secrets: %s removed from the dashboard", name)
    return {"name": name, "saved": False}
