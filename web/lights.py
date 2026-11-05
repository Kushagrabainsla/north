"""The dashboard's Lights page: your Kasa devices, controlled by you (CODING_STYLE §7.3).

A control you use here acts as you: it does what you set at once, with no approval
card, and reaches the device through `KasaDevices`, the one home the kasa tool also
uses (§7.5). north.log notes each change. Which lights are yours, as opposed to a
friend's on the same network, is kept in north's settings.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from config.env_file import remove_env_key, unstorable, write_env_key
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


class LiveStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[str] = Field(min_length=1, max_length=64)


class LiveFrame(BaseModel):
    """One light's colour and brightness for this moment of the music."""

    model_config = ConfigDict(extra="forbid")

    id: str
    hue: int = Field(ge=0, le=360)
    saturation: int = Field(ge=0, le=100)
    brightness: int = Field(ge=1, le=100)


class LiveFrames(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frames: list[LiveFrame] = Field(min_length=1, max_length=64)
    # A fade as long as the time between frames, so a bulb glides from one to the next.
    transition_ms: int = Field(120, ge=0, le=2000)


class KasaAccountUpdate(BaseModel):
    """A TP-Link login: the Kasa app account some of the bulbs were set up with."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


# Where each login is kept in north's .env: your account, and a second one (a friend's bulbs).
_ACCOUNT_KEYS = {
    "yours": ("NORTH_KASA_USERNAME", "NORTH_KASA_PASSWORD"),
    "second": ("NORTH_KASA_SECOND_USERNAME", "NORTH_KASA_SECOND_PASSWORD"),
}


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
        "locked": devices.locked,
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


@router.post("/lights/live/start")
async def start_music_sync(body: LiveStart) -> dict[str, list[str]]:
    """Note how the chosen lights are now, so stopping puts them back, and let them follow the music."""
    return {"following": await _devices().live.start(body.ids)}


@router.post("/lights/live", status_code=202)
async def music_frames(body: LiveFrames) -> dict[str, int]:
    """The dashboard heard the music and chose these colours: send each light its newest one.

    Only colours arrive here; the sound stays in the browser. Each frame is sent on at once and
    replaces any a bulb has not reached yet, so a slow bulb never falls behind the music.
    """
    frames = {
        f.id: LightChange(
            on=True, brightness=f.brightness, hue=f.hue, saturation=f.saturation, transition_ms=body.transition_ms
        )
        for f in body.frames
    }
    try:
        _devices().live.frame(frames)
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"frames": len(frames)}


@router.post("/lights/live/stop")
async def stop_music_sync() -> dict[str, bool]:
    """Stop following the music and put the lights back as they were."""
    await _devices().live.stop()
    return {"stopped": True}


@router.get("/lights/accounts")
async def kasa_accounts() -> dict[str, Any]:
    """The TP-Link logins north has for your bulbs, by email; a password is never sent back."""
    return {"accounts": _accounts()}


@router.put("/lights/accounts/{slot}")
async def save_kasa_account(slot: str, body: KasaAccountUpdate) -> dict[str, Any]:
    """Keep a TP-Link login in north's .env, then try it on the bulbs at once.

    Bulbs set up with the Kasa app answer only that account, and the login could only be
    set by editing north's .env by hand.
    """
    keys = _account_keys(slot)
    values = (body.email.strip(), body.password)
    for value in values:
        if why := unstorable(value):
            raise HTTPException(status_code=422, detail=f"North cannot keep this login exactly: {why}.")
    home = current_services().require("north_home")
    for key, value in zip(keys, values, strict=True):
        await asyncio.to_thread(write_env_key, home, key, value)
        os.environ[key] = value
    _reload_settings()
    devices = _devices()
    found = await devices.devices(look_again=True)
    logger.info("Lights: a TP-Link login was saved for %s; %d device(s) answer", slot, len(found))
    return {"accounts": _accounts(), "answering": len(found), "locked": devices.locked, "problem": devices.problem}


@router.delete("/lights/accounts/{slot}")
async def remove_kasa_account(slot: str) -> dict[str, Any]:
    """Forget a TP-Link login; bulbs already connected keep working until north restarts."""
    home = current_services().require("north_home")
    for key in _account_keys(slot):
        await asyncio.to_thread(remove_env_key, home, key)
        os.environ.pop(key, None)
    _reload_settings()
    return {"accounts": _accounts()}


def _account_keys(slot: str) -> tuple[str, str]:
    if slot not in _ACCOUNT_KEYS:
        raise HTTPException(status_code=404, detail="Unknown account; it is yours or second.")
    return _ACCOUNT_KEYS[slot]


def _accounts() -> list[dict[str, Any]]:
    from config.settings import settings

    pairs = {
        "yours": (settings.kasa_username, settings.kasa_password),
        "second": (settings.kasa_second_username, settings.kasa_second_password),
    }
    return [
        {"slot": slot, "email": email, "saved": bool(email and password)} for slot, (email, password) in pairs.items()
    ]


def _reload_settings() -> None:
    from config.settings import reload_settings

    reload_settings()


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
