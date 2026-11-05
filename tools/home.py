"""Your home devices from every brand: the one place the Devices page and music sync reach them (CODING_STYLE §7.5).

North ships one brand, Kasa (`tools/specialized/kasa_devices.py`). Any other comes as a
`DeviceTool`: a tool north writes when you add a device on the Devices page, such as a
Philips Hue bridge or a Govee strip. Every device is shown the same way, by what it can
do (switch on and off, dim, colour, white), so a Hue lamp gets the same switch, sliders,
scenes and music sync as a Kasa bulb. A brand's devices are named "<tool>/<id>"; Kasa's
keep the ids they always had, so the lights you marked as yours stay yours.

What you do on the page acts as you, with no approval card (CODING_STYLE §7.3); what north
does from chat goes through the tool, and asks as your mode says.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from abc import abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from config import secret_store
from tools.base import Tool
from tools.home_network import NetworkDevice, look
from tools.models import ToolInput, ToolOutput
from utils.tasks import spawn

if TYPE_CHECKING:
    from tools.registry import ToolRegistry
    from tools.specialized.kasa_devices import KasaDevices

logger = logging.getLogger(__name__)

KASA = "kasa"
# How long one brand may take to list its devices before the page shows the others without it.
_FIND_SECONDS = 15.0
# The longest fade between two streamed changes: past this a bulb that slow follows nothing anyway.
_LONGEST_FADE_MS = 1000


@dataclass(frozen=True)
class LightChange:
    """What to change on a device; None leaves that part as it is."""

    on: bool | None = None
    brightness: int | None = None  # 1-100
    hue: int | None = None  # 0-360
    saturation: int | None = None  # 0-100
    kelvin: int | None = None
    transition_ms: int | None = None  # a fade, for a device that can fade


# Presets for a mood, shared by every brand, from chat and on the Devices page.
SCENES: dict[str, LightChange] = {
    "moody": LightChange(brightness=30, kelvin=2700),
    "cozy": LightChange(brightness=35, kelvin=2700),
    "movie": LightChange(brightness=15, kelvin=2500),
    "focus": LightChange(brightness=90, kelvin=5000),
    "romantic": LightChange(brightness=25, hue=330, saturation=75),
    "party": LightChange(brightness=70, hue=300, saturation=100),
    "sunset": LightChange(brightness=40, kelvin=2500),
}


def change_of(
    on: bool | None = None,
    brightness: int | None = None,
    hue: int | None = None,
    saturation: int | None = None,
    kelvin: int | None = None,
    scene: str | None = None,
) -> LightChange:
    """The change a request asks for: a scene on its own, or any of power, brightness, colour and white.

    Raises ValueError, saying why, for a request that asks for nothing or makes no sense.
    """
    values = (on, brightness, hue, saturation, kelvin)
    if scene is not None:
        if any(value is not None for value in values):
            raise ValueError("a scene is applied on its own")
        if scene not in SCENES:
            raise ValueError(f"unknown scene; known: {', '.join(sorted(SCENES))}")
        return SCENES[scene]
    if all(value is None for value in values):
        raise ValueError("nothing to change")
    if (hue is None) != (saturation is None):
        raise ValueError("a colour needs both hue and saturation")
    return LightChange(on, brightness, hue, saturation, kelvin)


@dataclass(frozen=True)
class Device:
    """One device as its brand's tool reports it: what it is, what it can do, and how it is now.

    Leave out what it cannot do: a plug has only `on`. `white` is its range of white light
    in kelvin, (warmest, coolest).
    """

    id: str
    name: str
    on: bool
    dims: bool = False
    colors: bool = False
    white: tuple[int, int] | None = None
    brightness: int | None = None
    hue: int | None = None
    saturation: int | None = None
    kelvin: int | None = None

    def shown(self, source: str) -> dict[str, Any]:
        """The device as the Devices page and chat show it, under the id "<source>/<id>"."""
        light = self.dims or self.colors or self.white is not None
        entry: dict[str, Any] = {
            "id": f"{source}/{self.id}",
            "name": self.name,
            "host": "",
            "model": "",
            "on": bool(self.on),
            "kind": "light" if light else "switch",
            "can": {"dim": self.dims, "color": self.colors, "white": list(self.white) if self.white else None},
        }
        if self.dims and self.brightness is not None:
            entry["brightness"] = int(self.brightness)
        if self.white and self.kelvin:
            entry["kelvin"] = int(self.kelvin)
        elif self.colors and self.hue is not None:
            entry["hue"], entry["saturation"] = int(self.hue), int(self.saturation or 0)
        return entry


class DeviceTool(Tool):
    """One brand of home device. Write `find()` and `change()`; north does the rest.

    From chat it is a tool with two actions: `list`, and `set`, which asks first like any change.
    On the Devices page its devices show beside your Kasa bulbs, with the same controls and
    music sync. A key or password it needs is one of your secrets (`uses_secrets`).
    """

    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "set"],
                "description": "list: every device and how it is now. set: change the devices in `ids`.",
            },
            "ids": {"type": "array", "items": {"type": "string"}, "description": "For set: the ids list gave."},
            "on": {"type": "boolean"},
            "brightness": {"type": "integer", "minimum": 1, "maximum": 100},
            "hue": {"type": "integer", "minimum": 0, "maximum": 360, "description": "With saturation."},
            "saturation": {"type": "integer", "minimum": 0, "maximum": 100},
            "kelvin": {"type": "integer", "minimum": 1000, "maximum": 10000, "description": "White, warm to cool."},
            "scene": {"type": "string", "enum": sorted(SCENES), "description": "A preset mood, on its own."},
        },
        "required": ["action"],
    }

    @abstractmethod
    async def find(self) -> list[Device]:
        """Every device of this brand you can reach now, with how it is now."""

    @abstractmethod
    async def change(self, device_id: str, change: LightChange) -> Device:
        """Make *change* on one device (a field that is None stays as it is); return the device as it is after."""

    def mutates(self, params: dict[str, Any] | None = None) -> bool:
        return (params or {}).get("action", "list") != "list"

    async def run(self, input: ToolInput) -> ToolOutput:
        params = input.params
        action = params.get("action", "list")
        if action == "list":
            try:
                found = await self.find()
            except Exception as exc:  # noqa: BLE001 - the brand's own failure, said as it is
                return ToolOutput(success=False, error=f"Could not reach the {self.name} devices: {_why(exc)}")
            return ToolOutput(success=True, data={"devices": [device.shown(self.name) for device in found]})
        if action != "set":
            return ToolOutput(success=False, error="action is list or set.")
        ids = [_local(str(i), self.name) for i in params.get("ids") or []]
        if not ids:
            return ToolOutput(success=False, error="set needs `ids`: call action='list' for them.")
        try:
            change = change_of(*(params.get(k) for k in ("on", "brightness", "hue", "saturation", "kelvin", "scene")))
        except ValueError as exc:
            return ToolOutput(success=False, error=f"{exc}.")
        outcomes = await asyncio.gather(*(self.change(i, change) for i in ids), return_exceptions=True)
        devices = [o.shown(self.name) for o in outcomes if isinstance(o, Device)]
        errors = [f"{i}: {_why(o)}" for i, o in zip(ids, outcomes, strict=True) if isinstance(o, BaseException)]
        if errors and not devices:
            return ToolOutput(success=False, error="; ".join(errors))
        return ToolOutput(success=True, data={"devices": devices, "errors": errors})

    def format_output(self, data: dict[str, Any]) -> str:
        lines = [_line(device) for device in data.get("devices", [])] or [f"No {self.name} devices found."]
        return "\n".join(lines + [f"Not done: {error}" for error in data.get("errors", [])])


class Home:
    """Every device from every brand in one list, and the one way the page and music sync change them.

    Brands are read from the tool registry each time, so a brand north has just written shows
    up without a restart.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        idle_seconds: float = 5.0,
        find_on_network: Callable[[], Awaitable[list[NetworkDevice]]] | None = None,
    ) -> None:
        self._registry = registry
        self._find_on_network = find_on_network or look
        self._problems: list[str] = []
        # The newest change waiting for each device that is being streamed to, and whether it is a beat.
        self._latest: dict[str, tuple[LightChange, bool]] = {}
        self._senders: dict[str, asyncio.Task[None]] = {}
        self.live = LiveLights(self, idle_seconds)

    @property
    def problem(self) -> str:
        """Why some devices could not be listed, or "" when nothing is wrong."""
        return " ".join(self._problems)

    @property
    def locked(self) -> list[str]:
        """The Kasa bulbs that refused every TP-Link login north has."""
        kasa = self._kasa()
        return kasa.locked if kasa is not None else []

    async def devices(self, *, look_again: bool = False) -> list[dict[str, Any]]:
        """Every device of every brand, with what it can do and how it is now, each naming its brand."""
        kasa, brands = self._kasa(), self._brands()
        lists = await asyncio.gather(
            *([kasa.devices(look_again=look_again)] if kasa is not None else []),
            *(self._find(name, tool) for name, tool in brands.items()),
            return_exceptions=True,
        )
        sources = ([KASA] if kasa is not None else []) + list(brands)
        found: list[dict[str, Any]] = []
        problems: list[str] = []
        for source, listed in zip(sources, lists, strict=True):
            if isinstance(listed, BaseException):
                problems.append(f"{source}: {_why(listed)}")
                continue
            found += [device | {"source": source} for device in listed]
        # "Kasa found nothing" is news only while nothing else was found: with only Hue lamps
        # in the house, it stood above them every time the page opened.
        kasa_found = any(device["source"] == KASA for device in found)
        if kasa is not None and kasa.problem and (kasa_found or kasa.locked or not found):
            problems.insert(0, kasa.problem)
        self._problems = problems
        return found

    async def discover(self) -> dict[str, Any]:
        """Every device on the network, each saying what north knows of it and whether it is already on the page.

        Kasa devices are matched by address and named as Kasa names them; a recognised brand north already
        has a tool for is marked with that tool.
        """
        kasa = self._kasa()
        listed, kasa_devices = await asyncio.gather(
            self._find_on_network(),
            kasa.devices() if kasa is not None else asyncio.sleep(0, result=[]),
            return_exceptions=True,
        )
        if isinstance(listed, BaseException):
            raise listed
        kasa_listed = kasa_devices if isinstance(kasa_devices, list) else []  # Kasa down: listed, just not marked
        on_page = {device["host"]: device["name"] for device in kasa_listed if device.get("host")}
        brands = self._brands()
        for device in listed:
            if device.address in on_page:
                device.here = KASA
                device.names.insert(0, on_page[device.address])
            elif device.found is not None:
                device.here = next((name for name in brands if device.found.brand.key in name), "")
        return {"devices": [device.shown() for device in listed]}

    async def apply(self, device_id: str, change: LightChange, *, confirm: bool = True) -> dict[str, Any]:
        """Make *change* on one device of any brand and return how it is after.

        *confirm* is Kasa's: it reads the bulb back. A brand's tool returns the device as it is.
        """
        source, local = self._source_of(device_id)
        if source == KASA:
            kasa = self._kasa()
            if kasa is None:
                raise LookupError("Kasa is not available here.")
            return await kasa.apply(local, change, confirm=confirm) | {"source": KASA}
        tool = self._brands()[source]
        with secret_store.released_to(tool.name, tool.uses_secrets):
            device = await asyncio.wait_for(tool.change(local, change), _FIND_SECONDS)
        return device.shown(source) | {"source": source}

    def stream(self, device_id: str, change: LightChange, *, beat: bool = False) -> None:
        """Send *change* as soon as the device can take it, replacing any change it has not reached yet.

        For a light following music: a bulb that takes 200 ms per request gets the newest frame
        each time, so it never falls further and further behind what is playing. A *beat* is the
        exception: the frames after it (the flash fading) do not replace it while it waits, or a
        slow bulb would show the fading and never the flash.
        """
        waiting = self._latest.get(device_id)
        if beat or waiting is None or not waiting[1]:
            self._latest[device_id] = (change, beat)
        if device_id not in self._senders:
            self._senders[device_id] = spawn(self._send_newest(device_id), name=f"home-stream:{device_id}")

    def stop_streams(self) -> None:
        """Drop the changes not sent yet; a request already on its way still lands."""
        self._latest.clear()

    async def aclose(self) -> None:
        """Put back any lights following music, before north closes their connections."""
        await self.live.stop()

    async def _send_newest(self, device_id: str) -> None:
        previous: float | None = None
        try:
            while (waiting := self._latest.pop(device_id, None)) is not None:
                change, beat = waiting
                # Fade for as long as this bulb takes between changes, so it is still moving when the next
                # one comes. With a 120 ms fade, a bulb that took longer between changes jumped, held, and
                # jumped again: the user's KL125 and KL135 bulbs flickered. A beat keeps the quick rise
                # it asked for: stretched to the bulb's pace, the flash would blur into the fading.
                started = time.monotonic()
                if previous is not None and not beat:
                    taken = int((started - previous) * 1000)
                    change = replace(change, transition_ms=min(max(change.transition_ms or 0, taken), _LONGEST_FADE_MS))
                previous = started
                try:
                    await self.apply(device_id, change, confirm=False)
                except Exception:  # noqa: BLE001 - one frame lost is no failure; a silent bulb is not hammered
                    logger.debug("home: a streamed change to %s failed", device_id, exc_info=True)
                    await asyncio.sleep(0.5)
        finally:
            self._senders.pop(device_id, None)

    async def _find(self, source: str, tool: DeviceTool) -> list[dict[str, Any]]:
        with secret_store.released_to(tool.name, tool.uses_secrets):
            found = await asyncio.wait_for(tool.find(), _FIND_SECONDS)
        return [device.shown(source) for device in found]

    def _kasa(self) -> KasaDevices | None:
        with contextlib.suppress(Exception):
            return self._registry.get(KASA).devices  # type: ignore[attr-defined]
        return None

    def _brands(self) -> dict[str, DeviceTool]:
        """Every brand's tool by name: a tool north wrote is wrapped, and its own code is what lists devices."""
        brands: dict[str, DeviceTool] = {}
        for tool in self._registry.all_tools():
            inner = getattr(tool, "inner", tool)
            if isinstance(inner, DeviceTool):
                brands[tool.name] = inner
        return brands

    def _source_of(self, device_id: str) -> tuple[str, str]:
        source, _, local = device_id.partition("/")
        if local and source in self._brands():
            return source, local
        if local:
            raise LookupError(f"No brand {source!r} is set up: add the device again on the Devices page.")
        return KASA, device_id


class LiveLights:
    """Lights following something live, such as music, then put back as they were.

    Started from the dashboard, which sends frames from the music it hears; north only sends
    them on. If frames stop arriving (the tab was closed) the lights are put back by themselves.
    """

    def __init__(self, home: Home, idle_seconds: float = 5.0) -> None:
        self._home = home
        self._idle_seconds = idle_seconds
        self._before: dict[str, LightChange] = {}  # how each light was when it started following
        self._last_frame = 0.0
        self._watch: asyncio.Task[None] | None = None

    @property
    def following(self) -> list[str]:
        return list(self._before)

    async def start(self, device_ids: list[str]) -> list[str]:
        """Note how *device_ids* are now, so stopping puts them back; returns the ones found."""
        await self.stop()
        found = {d["id"]: d for d in await self._home.devices() if not d.get("unreachable")}
        self._before = {i: _as_change(found[i]) for i in device_ids if i in found}
        self._last_frame = time.monotonic()
        self._watch = spawn(self._stop_when_idle(), name="home-live-watch")
        logger.info("Devices: music sync started on %s", list(self._before))
        return self.following

    def frame(self, changes: dict[str, LightChange], *, beat: bool = False) -> None:
        """Send each following light its newest change; any other light is left alone.

        *beat* marks the frames of a beat, which the frames after them do not overtake.
        """
        if not self._before:
            raise LookupError("No lights are following anything: start first.")
        self._last_frame = time.monotonic()
        for device_id, change in changes.items():
            if device_id in self._before:
                self._home.stream(device_id, change, beat=beat)

    async def stop(self) -> None:
        """Stop following, and put every light back as it was."""
        if self._watch is not None and self._watch is not asyncio.current_task():
            self._watch.cancel()
        self._watch = None
        before, self._before = self._before, {}
        if not before:
            return
        self._home.stop_streams()
        await asyncio.gather(
            *(self._home.apply(i, change, confirm=False) for i, change in before.items()), return_exceptions=True
        )
        logger.info("Devices: music sync stopped; %s put back", list(before))

    async def _stop_when_idle(self) -> None:
        while self._before:
            await asyncio.sleep(min(1.0, self._idle_seconds / 2))
            if time.monotonic() - self._last_frame > self._idle_seconds:
                logger.info("Devices: no music frames for %.0f s; putting the lights back", self._idle_seconds)
                await self.stop()


def _as_change(state: dict[str, Any]) -> LightChange:
    """The change that brings a device back to *state*."""
    if not state["on"]:
        return LightChange(on=False)
    if state.get("hue") is not None:
        return LightChange(
            on=True, brightness=state.get("brightness"), hue=state["hue"], saturation=state["saturation"]
        )
    return LightChange(on=True, brightness=state.get("brightness"), kelvin=state.get("kelvin"))


def _local(device_id: str, source: str) -> str:
    """A device's own id, from either form chat may pass: "3" or "hue_lights/3"."""
    prefix = f"{source}/"
    return device_id[len(prefix) :] if device_id.startswith(prefix) else device_id


def _why(error: BaseException) -> str:
    if isinstance(error, TimeoutError):
        return f"no answer within {_FIND_SECONDS:.0f} s"
    return f"{type(error).__name__}: {error}" if str(error) else type(error).__name__


def _line(device: dict[str, Any]) -> str:
    parts = ["on" if device.get("on") else "off"]
    if device.get("brightness") is not None:
        parts.append(f"{device['brightness']}%")
    if device.get("kelvin"):
        parts.append(f"white {device['kelvin']}K")
    elif device.get("hue") is not None:
        parts.append(f"colour hue {device['hue']}, saturation {device.get('saturation')}%")
    return f"{device['name']} ({device['id']}): {', '.join(parts)}"
