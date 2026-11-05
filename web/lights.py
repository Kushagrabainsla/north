"""The dashboard's Lights page: your Kasa devices, controlled by you (CODING_STYLE §7.3).

A control you use here acts as you: it does what you set at once, with no approval
card, and reaches the device through `KasaDevices`, the one home the kasa tool also
uses (§7.5). north.log notes each change. Which lights are yours, as opposed to a
friend's on the same network, is kept in north's settings.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from orchestrator.api_context import current_services
from tools.specialized.kasa_devices import SCENES, KasaDevices, LightChange

logger = logging.getLogger(__name__)
router = APIRouter(tags=["web"])


class LightsApply(BaseModel):
    """A change to one light or several: a scene, or any of power, brightness, colour and white."""

    model_config = ConfigDict(extra="forbid")

    ids: list[str] = Field(min_length=1, max_length=64)
    on: bool | None = None
    brightness: int | None = Field(None, ge=1, le=100)
    hue: int | None = Field(None, ge=0, le=360)
    saturation: int | None = Field(None, ge=0, le=100)
    kelvin: int | None = Field(None, ge=1000, le=10_000)
    scene: str | None = None

    @model_validator(mode="after")
    def _one_change(self) -> LightsApply:
        values = (self.on, self.brightness, self.hue, self.saturation, self.kelvin)
        if self.scene is not None and any(value is not None for value in values):
            raise ValueError("a scene is applied on its own")
        if self.scene is None and all(value is None for value in values):
            raise ValueError("nothing to change")
        if self.scene is not None and self.scene not in SCENES:
            raise ValueError(f"unknown scene; known: {', '.join(sorted(SCENES))}")
        if (self.hue is None) != (self.saturation is None):
            raise ValueError("a colour needs both hue and saturation")
        return self

    def change(self) -> LightChange:
        if self.scene is not None:
            return SCENES[self.scene]
        return LightChange(self.on, self.brightness, self.hue, self.saturation, self.kelvin)


class MineUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mine: bool


@router.get("/lights")
async def list_lights(look_again: bool = False) -> dict[str, Any]:
    """Your devices, what each can do and its state, and which are yours."""
    devices = _devices()
    mine = set(_settings().my_lights)
    found = await devices.devices(look_again=look_again)
    return {
        "lights": [device | {"mine": device["id"] in mine} for device in found],
        "problem": devices.problem,
        "scenes": sorted(SCENES),
    }


@router.post("/lights/apply")
async def apply_lights(body: LightsApply) -> dict[str, Any]:
    """Change the lights you chose, together; each one that will not answer is named with why."""
    devices = _devices()
    change = body.change()
    outcomes = await asyncio.gather(*(devices.apply(i, change) for i in body.ids), return_exceptions=True)
    mine = set(_settings().my_lights)
    lights, errors = [], []
    for device_id, outcome in zip(body.ids, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            errors.append({"id": device_id, "error": str(outcome) or type(outcome).__name__})
        else:
            lights.append(outcome | {"mine": device_id in mine})
    logger.info("Lights: you set %s on %s", body.model_dump(exclude_none=True, exclude={"ids"}), body.ids)
    if errors and not lights:
        raise HTTPException(status_code=502, detail="; ".join(f"{e['id']}: {e['error']}" for e in errors))
    return {"lights": lights, "errors": errors}


@router.put("/lights/{device_id}/mine")
async def mark_mine(device_id: str, body: MineUpdate) -> dict[str, list[str]]:
    """Mark a light as yours, or not: "My lights" controls only these."""
    settings = _settings()
    mine = [i for i in settings.my_lights if i != device_id] + ([device_id] if body.mine else [])
    try:
        settings.set_my_lights(mine)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"mine": settings.my_lights}


def _devices() -> KasaDevices:
    from tools.exceptions import ToolNotFoundError

    try:
        return current_services().require("tool_registry").get("kasa").devices
    except ToolNotFoundError:
        raise HTTPException(status_code=503, detail="The Kasa tool is not available here.") from None


def _settings():
    return current_services().require("north_settings")
