"""KasaTool — control TP-Link Kasa smart bulbs over the local network.

Uses python-kasa for device control. Discovery runs the `kasa discover` CLI
as a subprocess (the only reliable method inside uvicorn's event loop, since
UDP broadcast discovery conflicts with the running asyncio loop on macOS).
Device control after discovery uses the async python-kasa API directly.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import sys
from typing import Any

from tools.base import Tool
from tools.models import ToolInput, ToolOutput

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


def _run_kasa_discover() -> list[tuple[str, str]]:
    """Run `kasa discover` as a subprocess. Returns [(ip, alias), ...]."""
    kasa_bin = shutil.which("kasa") or f"{sys.executable.rsplit('/', 1)[0]}/kasa"
    try:
        result = subprocess.run(
            [kasa_bin, "discover"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        output = result.stdout
    except FileNotFoundError:
        result = subprocess.run(
            [sys.executable, "-m", "kasa", "discover"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        output = result.stdout
    except subprocess.TimeoutExpired:
        return []

    hosts = re.findall(r"Host:\s+(\d+\.\d+\.\d+\.\d+)", output)
    aliases = re.findall(r"==\s+(.+?)\s+-\s+\w+\s+==", output)
    return [(host, aliases[i] if i < len(aliases) else host) for i, host in enumerate(hosts)]


async def _connect_devices(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """Connect to each discovered device. Returns {ip: device}."""
    from kasa import Device

    found = {}
    for host, _ in pairs:
        try:
            dev = await Device.connect(host=host)
            await dev.update()
            found[host] = dev
        except Exception:
            pass
    return found


class KasaTool(Tool):
    """Discover and control TP-Link Kasa smart bulbs on the local network."""

    name = "kasa"
    description = (
        "Control TP-Link Kasa smart bulbs over the local network. "
        "Supports on/off/toggle, brightness, colour (by name or hue), "
        "colour temperature, and listing devices. "
        "Specify a device alias or IP to target one bulb; omit to affect all discovered bulbs."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["on", "off", "toggle", "list", "brightness", "color", "color_temp"],
                "description": (
                    "'on'/'off'/'toggle' — power control. "
                    "'brightness' — set brightness (requires brightness param). "
                    "'color' — set colour by name or hue/saturation (requires color or hue param). "
                    "'color_temp' — set white colour temperature (requires color_temp param). "
                    "'list' — show all discovered devices and their state."
                ),
            },
            "device": {
                "type": "string",
                "description": ("Device alias (e.g. 'Desk lamp') or IP address. Omit to target all discovered bulbs."),
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
        },
        "required": ["action"],
    }

    def format_output(self, data: dict[str, Any]) -> str:
        devices = data.get("devices", [])
        if not devices:
            return data.get("message", "No Kasa devices found on the network.")
        blocks = []
        for d in devices:
            status = "on" if d.get("is_on") else "off"
            header = f"**{d['alias']}** ({d['host']}) — {status}"
            attrs = []
            if d.get("brightness") is not None:
                attrs.append(f"- Brightness: {d['brightness']}%")
            if d.get("color_temp"):
                attrs.append(f"- Color temp: {d['color_temp']}K")
            if d.get("hue") is not None:
                attrs.append(f"- Hue: {d['hue']}  Saturation: {d.get('saturation')}%")
            blocks.append(header + ("\n" + "\n".join(attrs) if attrs else ""))
        if msg := data.get("message"):
            blocks.insert(0, msg)
        return "\n\n".join(blocks)

    async def run(self, input: ToolInput) -> ToolOutput:
        action = input.params.get("action")
        if not action:
            return ToolOutput(success=False, error="Parameter 'action' is required.")

        try:
            import kasa  # noqa: F401
        except ImportError:
            return ToolOutput(
                success=False,
                error="python-kasa is not installed. Run: uv add python-kasa",
            )

        try:
            pairs = await asyncio.to_thread(_run_kasa_discover)
        except Exception as exc:
            return ToolOutput(success=False, error=f"Discovery subprocess failed: {exc}")

        if not pairs:
            return ToolOutput(
                success=True,
                data={"devices": [], "message": "No Kasa devices found on the network."},
            )

        try:
            found = await _connect_devices(pairs)
        except Exception as exc:
            return ToolOutput(success=False, error=f"Failed to connect to devices: {exc}")

        if not found:
            return ToolOutput(
                success=True,
                data={"devices": [], "message": "Devices discovered but could not connect to any."},
            )

        alias_map = dict(pairs)
        target_hint = input.params.get("device", "").strip().lower()

        if target_hint:
            matched = {
                ip: dev
                for ip, dev in found.items()
                if target_hint in ip.lower()
                or target_hint in (dev.alias or "").lower()
                or target_hint in alias_map.get(ip, "").lower()
            }
            if not matched:
                names = [alias_map.get(ip, dev.alias or ip) for ip, dev in found.items()]
                return ToolOutput(
                    success=False,
                    error=f"No device matching {target_hint!r}. Found: {', '.join(names)}",
                )
        else:
            matched = found

        if action == "list":
            device_list = []
            for ip, dev in found.items():
                entry: dict[str, Any] = {
                    "alias": alias_map.get(ip, dev.alias or ip),
                    "host": ip,
                    "is_on": dev.is_on,
                    "model": getattr(dev, "model", "unknown"),
                }
                if (br := getattr(dev, "brightness", None)) is not None:
                    entry["brightness"] = br
                if ct := getattr(dev, "color_temp", None):
                    entry["color_temp"] = ct
                hsv = getattr(dev, "hsv", None)
                if hsv:
                    entry["hue"] = hsv.hue
                    entry["saturation"] = hsv.saturation
                device_list.append(entry)
            return ToolOutput(success=True, data={"devices": device_list})

        # Resolve colour / brightness params before the device loop
        hue: int | None = None
        saturation: int = 100
        kelvin: int | None = None
        brightness_val: int | None = None

        if action == "color":
            color_name = input.params.get("color", "").strip().lower()
            if color_name:
                if color_name not in _COLOR_NAMES:
                    known = ", ".join(_COLOR_NAMES)
                    return ToolOutput(success=False, error=f"Unknown colour {color_name!r}. Known: {known}")
                hue, saturation = _COLOR_NAMES[color_name]
            elif (raw_hue := input.params.get("hue")) is not None:
                try:
                    hue = int(raw_hue)
                    saturation = int(input.params.get("saturation", 100))
                except (ValueError, TypeError):
                    return ToolOutput(success=False, error="'hue' and 'saturation' must be integers.")
            else:
                return ToolOutput(success=False, error="action='color' requires 'color' (name) or 'hue'.")

        elif action == "color_temp":
            raw_ct = str(input.params.get("color_temp", "")).strip().lower()
            if not raw_ct:
                return ToolOutput(success=False, error="action='color_temp' requires 'color_temp'.")
            if raw_ct in _COLOR_TEMPS:
                kelvin = _COLOR_TEMPS[raw_ct]
            else:
                try:
                    kelvin = int(raw_ct)
                    if not (2500 <= kelvin <= 6500):
                        return ToolOutput(success=False, error="color_temp must be 2500–6500 K.")
                except ValueError:
                    known = ", ".join(_COLOR_TEMPS)
                    return ToolOutput(
                        success=False,
                        error=f"Unknown color_temp {raw_ct!r}. Use: {known} or a number 2500–6500.",
                    )

        elif action == "brightness":
            raw_br = input.params.get("brightness")
            if raw_br is None:
                return ToolOutput(success=False, error="action='brightness' requires 'brightness' (0–100).")
            try:
                brightness_val = max(0, min(100, int(raw_br)))
            except (ValueError, TypeError):
                return ToolOutput(success=False, error="'brightness' must be an integer 0–100.")

        results = []
        errors = []
        for ip, dev in matched.items():
            alias = alias_map.get(ip, dev.alias or ip)
            try:
                if action == "on":
                    await dev.turn_on()
                elif action == "off":
                    await dev.turn_off()
                elif action == "toggle":
                    await dev.turn_off() if dev.is_on else await dev.turn_on()
                elif action == "brightness":
                    await dev.set_brightness(brightness_val)
                elif action == "color":
                    await dev.set_hsv(hue, saturation, 100)
                elif action == "color_temp":
                    await dev.set_color_temp(kelvin)

                await dev.update()
                entry: dict[str, Any] = {"alias": alias, "host": ip, "is_on": dev.is_on}
                if (br := getattr(dev, "brightness", None)) is not None:
                    entry["brightness"] = br
                if ct := getattr(dev, "color_temp", None):
                    entry["color_temp"] = ct
                hsv = getattr(dev, "hsv", None)
                if hsv and action == "color":
                    entry["hue"] = hsv.hue
                    entry["saturation"] = hsv.saturation
                results.append(entry)
            except Exception as exc:
                errors.append(f"{alias}: {exc}")

        if errors and not results:
            return ToolOutput(success=False, error="; ".join(errors))

        _VERBS = {
            "on": "Turned on",
            "off": "Turned off",
            "toggle": "Toggled",
            "brightness": f"Set brightness to {brightness_val}%",
            "color": f"Set colour (hue={hue}, sat={saturation}%)",
            "color_temp": f"Set colour temperature to {kelvin}K",
        }
        names = ", ".join(r["alias"] for r in results)
        message = f"{_VERBS.get(action, action)}: {names}." + (f" Errors: {'; '.join(errors)}" if errors else "")
        return ToolOutput(success=True, data={"devices": results, "message": message})
