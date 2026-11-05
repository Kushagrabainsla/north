"""KasaTool - control TP-Link Kasa smart-home devices from chat.

The tool reads what you asked for (an action, a colour or warmth by name, a device by
name) and puts it to the approval layer. Reaching the devices is `KasaDevices`' job, the
one home it shares with the dashboard's Lights page (CODING_STYLE §7.5).
"""

from __future__ import annotations

import logging
from typing import Any

from approval.approvals import Request
from approval.policy import Action, ActionKind
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.specialized.kasa_devices import SCENES, KasaDevices, LightChange

logger = logging.getLogger(__name__)

# Named colours → (hue 0-360, saturation 0-100)
_COLOR_NAMES: dict[str, tuple[int, int]] = {
    "red": (0, 100),
    "orange": (30, 100),
    "yellow": (60, 100),
    "green": (120, 100),
    "cyan": (180, 100),
    "blue": (240, 100),
    "purple": (270, 100),
    "violet": (270, 100),
    "pink": (300, 100),
    "magenta": (300, 100),
}

# Named colour temperatures → Kelvin
_COLOR_TEMPS: dict[str, int] = {
    "candlelight": 2500,
    "warm": 2700,
    "soft": 3000,
    "neutral": 4000,
    "cool": 5000,
    "daylight": 6500,
}

_ACTION_ALIASES = {
    "turn_on": "on",
    "turn_off": "off",
    # "dim my lights to 50%" is how a person says this, and the word reached the
    # tool as an action nobody had defined - which the guard below then reported
    # as a missing `device`, blaming the request for the vocabulary.
    "dim": "brightness",
    "brighten": "brightness",
    "set_level": "brightness",
    "level": "brightness",
    "set_brightness": "brightness",
    "set_color": "color",
    "set_colour": "color",
    "set_color_temp": "color_temp",
    "set_color_temperature": "color_temp",
    "apply_scene": "scene",
    "mood": "scene",
}

# Every action the tool answers to, after aliasing. Kept beside the aliases so a
# new action cannot be added to one and forgotten in the other.
_KNOWN_ACTIONS = frozenset({"on", "off", "toggle", "list", "brightness", "color", "color_temp", "scene"})

# Valid colour-temperature range for Kasa bulbs, in Kelvin.
_KELVIN_MIN = 2500
_KELVIN_MAX = 6500


def _action_of(params: dict[str, Any]) -> str:
    """The requested action, lowercased and with aliases resolved."""
    action = str(params.get("action", "")).strip().lower()
    return _ACTION_ALIASES.get(action, action)


def _change_for(action: str, params: dict[str, Any]) -> LightChange:
    """What *action* with *params* asks a device to become (toggle is decided per device).

    Raises:
        ValueError: with a user-facing message when a required param is missing or invalid.
    """
    if action in ("on", "off"):
        return LightChange(on=action == "on")
    if action == "color":
        color_name = str(params.get("color") or "").strip().lower()
        if color_name:
            if color_name not in _COLOR_NAMES:
                raise ValueError(f"Unknown colour {color_name!r}. Known: {', '.join(_COLOR_NAMES)}")
            hue, saturation = _COLOR_NAMES[color_name]
            return LightChange(hue=hue, saturation=saturation)
        if (raw_hue := params.get("hue")) is not None:
            try:
                return LightChange(hue=int(raw_hue), saturation=int(params.get("saturation", 100)))
            except (ValueError, TypeError):
                raise ValueError("'hue' and 'saturation' must be integers.") from None
        raise ValueError("action='color' requires 'color' (name) or 'hue'.")
    if action == "color_temp":
        raw_ct = str(params.get("color_temp", "")).strip().lower()
        if not raw_ct:
            raise ValueError("action='color_temp' requires 'color_temp'.")
        if raw_ct in _COLOR_TEMPS:
            return LightChange(kelvin=_COLOR_TEMPS[raw_ct])
        try:
            kelvin = int(raw_ct)
        except ValueError:
            raise ValueError(
                f"Unknown color_temp {raw_ct!r}. "
                f"Use: {', '.join(_COLOR_TEMPS)} or a number {_KELVIN_MIN}–{_KELVIN_MAX}."
            ) from None
        if not (_KELVIN_MIN <= kelvin <= _KELVIN_MAX):
            raise ValueError(f"color_temp must be {_KELVIN_MIN}–{_KELVIN_MAX} K.")
        return LightChange(kelvin=kelvin)
    if action == "brightness":
        raw_br = params.get("brightness")
        if raw_br is None:
            raise ValueError("action='brightness' requires 'brightness' (0–100).")
        try:
            brightness = max(0, min(100, int(raw_br)))
        except (ValueError, TypeError):
            raise ValueError("'brightness' must be an integer 0–100.") from None
        # 0 is how a bulb is told to switch off.
        return LightChange(on=False) if brightness == 0 else LightChange(brightness=brightness)
    if action == "scene":
        return SCENES[str(params.get("scene", "")).strip().lower()]
    return LightChange()


def _summary(action: str, params: dict[str, Any], results: list[dict[str, Any]]) -> str:
    """What was done, to whom, for the result."""
    names = ", ".join(r["name"] for r in results)
    colour = params.get("color") or f"hue {params.get('hue')}"
    done = {
        "on": "Turned on",
        "off": "Turned off",
        "toggle": "Toggled",
        "brightness": f"Set brightness to {params.get('brightness')}%",
        "color": f"Set colour to {colour}",
        "color_temp": f"Set colour temperature to {params.get('color_temp')}",
        "scene": f"Applied the {str(params.get('scene', '')).strip().lower()} scene",
    }
    return f"{done.get(action, action)}: {names}."


class KasaTool(Tool):
    """Discover and control TP-Link Kasa smart-home devices on the local network."""

    name = "kasa"
    is_mutating = True
    description = (
        "Control TP-Link Kasa smart-home devices - plugs, wall switches, bulbs, and light "
        "strips - on the local network, and list what is discovered. Every device supports "
        "on/off/toggle; brightness needs a dimmer or bulb, and colour or colour-temperature "
        "needs a colour/tunable-white bulb (the tool reports if a device lacks a feature). "
        "Identify the target by its alias/name (e.g. 'Desk lamp') or IP; if you do not know "
        "it, run action='list' first. Named scenes (moody, cozy, movie, focus, romantic, party, "
        "sunset) may target all discovered lights when no device is supplied. "
        "Control actions follow the approval mode; listing never asks."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["on", "off", "toggle", "list", "brightness", "color", "color_temp", "scene"],
                "description": (
                    "'on'/'off'/'toggle' - power control. "
                    "'brightness' - set brightness (requires brightness param). "
                    "'color' - set colour by name or hue/saturation (requires color or hue param). "
                    "'color_temp' - set white colour temperature (requires color_temp param). "
                    "'list' - show all discovered devices and their state. "
                    "'scene' - apply a preset mood to one device or all discovered devices."
                ),
            },
            "device": {
                "type": "string",
                "description": (
                    "Device alias (e.g. 'Desk lamp') or IP address. "
                    "Required for every action except 'list' - there is no implicit 'all devices' target."
                ),
            },
            "brightness": {
                "type": "integer",
                "description": "Brightness level 0–100. Used with action='brightness'.",
                "minimum": 0,
                "maximum": 100,
            },
            "color": {
                "type": "string",
                "description": (
                    "Colour name: red, orange, yellow, green, cyan, blue, purple, pink, magenta. "
                    "Used with action='color'."
                ),
            },
            "hue": {
                "type": "integer",
                "description": "Hue 0–360. Used with action='color' as an alternative to color name.",
                "minimum": 0,
                "maximum": 360,
            },
            "saturation": {
                "type": "integer",
                "description": "Saturation 0–100. Used with action='color' alongside hue. Defaults to 100.",
                "minimum": 0,
                "maximum": 100,
            },
            "color_temp": {
                "type": "string",
                "description": (
                    "Colour temperature: candlelight (2500K), warm (2700K), soft (3000K), "
                    "neutral (4000K), cool (5000K), daylight (6500K). "
                    "Or pass a number in Kelvin (2500–6500). "
                    "Used with action='color_temp'."
                ),
            },
            "scene": {
                "type": "string",
                "enum": sorted(SCENES),
                "description": "Preset mood: moody, cozy, movie, focus, romantic, party, or sunset.",
            },
        },
        "required": ["action"],
    }

    def __init__(self, devices: KasaDevices | None = None) -> None:
        # Shared with the dashboard's Lights page, which reaches it through this tool in the registry.
        self.devices = devices or KasaDevices()

    async def aclose(self) -> None:
        await self.devices.aclose()

    def format_output(self, data: dict[str, Any]) -> str:
        devices = data.get("devices", [])
        if not devices:
            return data.get("message", "No Kasa devices found on the network.")
        blocks = []
        for d in devices:
            header = f"**{d['name']}** ({d['host']}) - {'on' if d.get('on') else 'off'}"
            attrs = []
            if d.get("brightness") is not None:
                attrs.append(f"- Brightness: {d['brightness']}%")
            if d.get("kelvin"):
                attrs.append(f"- Color temp: {d['kelvin']}K")
            if d.get("hue") is not None:
                attrs.append(f"- Hue: {d['hue']}  Saturation: {d.get('saturation')}%")
            if d.get("unreachable"):
                attrs.append(f"- Not answering: {d['unreachable']}")
            blocks.append(header + ("\n" + "\n".join(attrs) if attrs else ""))
        if msg := data.get("message"):
            blocks.insert(0, msg)
        return "\n\n".join(blocks)

    def mutates(self, params: dict[str, Any] | None = None) -> bool:
        return _action_of(params or {}) != "list"

    async def describe(self, input: ToolInput) -> Request | None:
        action = _action_of(input.params)
        if action not in _KNOWN_ACTIONS:
            return None
        device = str(input.params.get("device", "")).strip() or "all discovered lights"
        return Request(
            # Devices are matched by name whatever its case, so "Living room lights" is the same action.
            action=Action(
                agent="kasa",
                kind=ActionKind.DEVICE,
                summary=f"{action} {device}",
                operation=action,
                args=device.casefold(),
            ),
            title="Device Control - Approval Required",
            message=f"`{action}` on **{device}**",
            declined="Device action cancelled by user.",
        )

    async def run(self, input: ToolInput) -> ToolOutput:
        action = _action_of(input.params)
        if not action:
            return ToolOutput(success=False, error="Parameter 'action' is required.")

        # An action nobody defined is that, and saying so: the device guard below
        # runs first and answered "device is required" for `dim`, `banana` and
        # every other unknown word alike.
        if action not in _KNOWN_ACTIONS:
            return ToolOutput(
                success=False,
                error=f"Unknown action {action!r}. Valid actions: {', '.join(sorted(_KNOWN_ACTIONS))}.",
            )

        target = str(input.params.get("device", "")).strip().lower()
        # Broad lighting requests are common in natural language. Allow
        # feature controls without a target to apply to all discovered
        # devices; keep power actions target-specific to avoid surprises with
        # plugs and switches.
        broad_actions = {"brightness", "color", "color_temp", "scene"}
        # `list` names nothing because it changes nothing - and it is the action
        # the error below tells the reader to run, so requiring a device for it
        # made the only route out of the error the error itself.
        if action != "list" and action not in broad_actions and not target:
            # Require an explicit target so a control action can never fan out
            # to every device on the network.
            return ToolOutput(
                success=False,
                error=(
                    "Parameter 'device' is required for control actions. "
                    "Use action='list' to see available devices, then target one by alias or IP."
                ),
            )
        if action == "scene" and str(input.params.get("scene", "")).strip().lower() not in SCENES:
            return ToolOutput(
                success=False,
                error=f"Unknown scene {input.params.get('scene')!r}. Available: {', '.join(sorted(SCENES))}.",
            )
        try:
            change = _change_for(action, input.params)
        except ValueError as exc:
            return ToolOutput(success=False, error=str(exc))

        found = await self.devices.devices()
        if not found:
            problem = self.devices.problem
            message = f"No Kasa devices found. {problem}" if problem else "No Kasa devices found on the network."
            return ToolOutput(
                success=not problem, data={"devices": [], "message": message}, error=message if problem else None
            )
        if action == "list":
            note = {"message": self.devices.problem} if self.devices.problem else {}
            return ToolOutput(success=True, data={"devices": found, **note})

        matched = [d for d in found if not target or target in d["host"].lower() or target in d["name"].lower()]
        if not matched:
            return ToolOutput(
                success=False,
                error=f"No device matching {target!r}. Found: {', '.join(d['name'] for d in found)}",
            )
        results, errors = [], []
        for device in matched:
            try:
                wanted = LightChange(on=not device["on"]) if action == "toggle" else change
                results.append(await self.devices.apply(device["id"], wanted))
            except Exception as exc:  # noqa: BLE001 - one device that fails must not hide the others
                errors.append(f"{device['name']}: {exc}")
        if errors and not results:
            return ToolOutput(success=False, error="; ".join(errors))
        message = _summary(action, input.params, results) + (f" Errors: {'; '.join(errors)}" if errors else "")
        return ToolOutput(success=True, data={"devices": results, "message": message})
