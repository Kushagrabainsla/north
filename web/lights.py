"""The dashboard's Devices page: your lights, plugs and switches of every brand, controlled by you (§7.3).

A control you use here acts as you: it does what you set at once, with no approval
card, and reaches the device through `tools.home.Home`, the one home for devices of
every brand (§7.5): Kasa, which north ships, and each brand north wrote a tool for when
you added a device. north.log notes each change. Which lights are yours, as opposed to
a friend's on the same network, is kept in north's settings. The TP-Link logins here
are Kasa's own.
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
from tools.home import SCENES, Home, LightChange, change_of

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
        self.change()  # raises ValueError for a change that asks for nothing or makes no sense
        return self

    def change(self) -> LightChange:
        return change_of(self.on, self.brightness, self.hue, self.saturation, self.kelvin, self.scene)


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
    # The frames of a beat: the ones after them do not overtake them on a slow bulb.
    beat: bool = False


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
    """Your devices of every brand, what each can do and its state, and which are yours."""
    home = _home()
    mine = set(_settings().my_lights)
    found = await home.devices(look_again=look_again)
    return {
        "lights": [device | {"mine": device["id"] in mine} for device in found],
        "problem": home.problem,
        "locked": home.locked,
        "scenes": sorted(SCENES),
    }


@router.post("/lights/apply")
async def apply_lights(body: LightsApply) -> dict[str, Any]:
    """Change the lights you chose, together; each one that will not answer is named with why."""
    home = _home()
    change = body.change()
    outcomes = await asyncio.gather(*(home.apply(i, change) for i in body.ids), return_exceptions=True)
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
    return {"following": await _home().live.start(body.ids)}


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
        _home().live.frame(frames, beat=body.beat)
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"frames": len(frames)}


@router.post("/lights/live/stop")
async def stop_music_sync() -> dict[str, bool]:
    """Stop following the music and put the lights back as they were."""
    await _home().live.stop()
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
    home = _home()
    found = [device for device in await home.devices(look_again=True) if device["source"] == "kasa"]
    logger.info("Lights: a TP-Link login was saved for %s; %d device(s) answer", slot, len(found))
    return {"accounts": _accounts(), "answering": len(found), "locked": home.locked, "problem": home.problem}


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


@router.put("/lights/{device_id:path}/mine")
async def mark_mine(device_id: str, body: MineUpdate) -> dict[str, list[str]]:
    """Mark a light as yours, or not: "My lights" controls only these."""
    settings = _settings()
    mine = [i for i in settings.my_lights if i != device_id] + ([device_id] if body.mine else [])
    try:
        settings.set_my_lights(mine)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"mine": settings.my_lights}


def _home() -> Home:
    home = current_services().home
    if home is None:
        raise HTTPException(status_code=503, detail="Home devices are not available here.")
    return home


def _settings():
    return current_services().require("north_settings")
