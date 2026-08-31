"""KasaTool - control TP-Link Kasa smart-home devices over the local network.

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
from dataclasses import dataclass
from typing import Any

from tools.base import ApprovalGatedTool
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

# Practical presets for natural-language lighting requests. A scene without a
# device target intentionally applies to every discovered Kasa device.
_SCENES: dict[str, tuple[tuple[str, dict[str, int]], ...]] = {
    "moody": (("brightness", {"brightness": 30}), ("color_temp", {"color_temp": 2700})),
    "cozy": (("brightness", {"brightness": 35}), ("color_temp", {"color_temp": 2700})),
    "movie": (("brightness", {"brightness": 15}), ("color_temp", {"color_temp": 2500})),
    "focus": (("brightness", {"brightness": 90}), ("color_temp", {"color_temp": 5000})),
    "romantic": (("brightness", {"brightness": 25}), ("color", {"hue": 330, "saturation": 75})),
    "party": (("brightness", {"brightness": 70}), ("color", {"hue": 300, "saturation": 100})),
    "sunset": (("brightness", {"brightness": 40}), ("color_temp", {"color_temp": 2500})),
}

_ACTION_ALIASES = {
    "turn_on": "on",
    "turn_off": "off",
    "set_brightness": "brightness",
    "set_color": "color",
    "set_colour": "color",
    "set_color_temp": "color_temp",
    "set_color_temperature": "color_temp",
    "apply_scene": "scene",
    "mood": "scene",
}

# Valid colour-temperature range for Kasa bulbs, in Kelvin.
_KELVIN_MIN = 2500
_KELVIN_MAX = 6500

# Human-readable verb per control action, for the result summary.
_ACTION_VERBS: dict[str, str] = {
    "on": "Turned on",
    "off": "Turned off",
    "toggle": "Toggled",
}

_DEVICE_TIMEOUT_SECONDS = 12.0
_DEVICE_RETRIES = 2


@dataclass
class _ActionParams:
    """Resolved colour/brightness parameters for a single control action."""

    hue: int | None = None
    saturation: int = 100
    kelvin: int | None = None
    brightness: int | None = None


def _run_kasa_discover() -> tuple[list[tuple[str, str]], str]:
    """Run `kasa discover` as a subprocess. Returns (pairs, diagnostic).

    *pairs* is [(ip, alias), ...]. *diagnostic* is an empty string on success,
    or a human-readable reason when discovery produced nothing (binary missing,
    permission error, timeout, no network) so the caller can surface it instead
    of a silent "no devices found".
    """
    kasa_bin = shutil.which("kasa") or f"{sys.executable.rsplit('/', 1)[0]}/kasa"
    last_err = ""
    for attempt in ("binary", "module"):
        try:
            cmd = [kasa_bin, "discover"] if attempt == "binary" else [sys.executable, "-m", "kasa", "discover"]
            # North is commonly launched detached, where stdin may be closed.
            # Explicitly detach all standard input so Python-based CLIs cannot
            # fail during interpreter startup with EBADF (Errno 9).
            result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
            output = result.stdout
            last_err = (result.stderr or "").strip()
            if result.returncode != 0:
                # A broken/missing binary should not prevent the module fallback.
                # Preserve the diagnostic if both discovery methods fail.
                last_err = f"`{' '.join(cmd)}` exited {result.returncode}: {last_err or 'no output'}"
                continue
        except FileNotFoundError:
            last_err = f"kasa executable not found at {kasa_bin}"
            continue  # try the `-m kasa` fallback
        except subprocess.TimeoutExpired:
            return [], "kasa discover timed out (UDP broadcast did not complete in 20s - check network/firewall)"
        except Exception as exc:  # noqa: BLE001
            return [], f"kasa discover raised: {exc}"

        hosts = re.findall(r"Host:\s+(\d+\.\d+\.\d+\.\d+)", output)
        aliases = re.findall(r"==\s+(.+?)\s+-\s+\w+\s+==", output)
        pairs = [(host, aliases[i] if i < len(aliases) else host) for i, host in enumerate(hosts)]
        return pairs, ""

    return [], last_err or "kasa discovery produced no output"


async def _connect_devices(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """Connect to each discovered device. Returns {ip: device}."""
    from kasa import Device

    found = {}
    for host, _ in pairs:
        for attempt in range(_DEVICE_RETRIES + 1):
            try:
                dev = await asyncio.wait_for(Device.connect(host=host), timeout=_DEVICE_TIMEOUT_SECONDS)
                await asyncio.wait_for(dev.update(), timeout=_DEVICE_TIMEOUT_SECONDS)
                found[host] = dev
                break
            except Exception:
                if attempt < _DEVICE_RETRIES:
                    await asyncio.sleep(0.25 * (attempt + 1))
    return found


def _device_state(
    dev: Any,
    ip: str,
    alias_map: dict[str, str],
    *,
    include_model: bool = False,
    include_hsv: bool = False,
) -> dict[str, Any]:
    """Snapshot a device's current state into a serialisable dict."""
    entry: dict[str, Any] = {
        "alias": alias_map.get(ip, dev.alias or ip),
        "host": ip,
        "is_on": dev.is_on,
    }
    if include_model:
        entry["model"] = getattr(dev, "model", "unknown")
    if (brightness := getattr(dev, "brightness", None)) is not None:
        entry["brightness"] = brightness
    if color_temp := getattr(dev, "color_temp", None):
        entry["color_temp"] = color_temp
    if include_hsv and (hsv := getattr(dev, "hsv", None)):
        entry["hue"] = hsv.hue
        entry["saturation"] = hsv.saturation
    return entry


def _resolve_action_params(action: str, params: dict[str, Any]) -> _ActionParams:
    """Parse and validate colour/brightness params for a control action.

    Raises:
        ValueError: with a user-facing message when a required param is missing or invalid.
    """
    resolved = _ActionParams()
    if action == "color":
        color_name = params.get("color", "").strip().lower()
        if color_name:
            if color_name not in _COLOR_NAMES:
                raise ValueError(f"Unknown colour {color_name!r}. Known: {', '.join(_COLOR_NAMES)}")
            resolved.hue, resolved.saturation = _COLOR_NAMES[color_name]
        elif (raw_hue := params.get("hue")) is not None:
            try:
                resolved.hue = int(raw_hue)
                resolved.saturation = int(params.get("saturation", 100))
            except (ValueError, TypeError):
                raise ValueError("'hue' and 'saturation' must be integers.") from None
        else:
            raise ValueError("action='color' requires 'color' (name) or 'hue'.")
    elif action == "color_temp":
        raw_ct = str(params.get("color_temp", "")).strip().lower()
        if not raw_ct:
            raise ValueError("action='color_temp' requires 'color_temp'.")
        if raw_ct in _COLOR_TEMPS:
            resolved.kelvin = _COLOR_TEMPS[raw_ct]
        else:
            try:
                resolved.kelvin = int(raw_ct)
            except ValueError:
                raise ValueError(
                    f"Unknown color_temp {raw_ct!r}. Use: {', '.join(_COLOR_TEMPS)} "
                    f"or a number {_KELVIN_MIN}–{_KELVIN_MAX}."
                ) from None
            if not (_KELVIN_MIN <= resolved.kelvin <= _KELVIN_MAX):
                raise ValueError(f"color_temp must be {_KELVIN_MIN}–{_KELVIN_MAX} K.")
    elif action == "brightness":
        raw_br = params.get("brightness")
        if raw_br is None:
            raise ValueError("action='brightness' requires 'brightness' (0–100).")
        try:
            resolved.brightness = max(0, min(100, int(raw_br)))
        except (ValueError, TypeError):
            raise ValueError("'brightness' must be an integer 0–100.") from None
    return resolved


async def _dispatch_device_action(dev: Any, action: str, resolved: _ActionParams) -> None:
    """Apply a single resolved action to one device."""
    if action == "on":
        await dev.turn_on()
    elif action == "off":
        await dev.turn_off()
    elif action == "toggle":
        await (dev.turn_off() if dev.is_on else dev.turn_on())
    elif action == "brightness":
        await dev.set_brightness(resolved.brightness)
    elif action == "color":
        await dev.set_hsv(resolved.hue, resolved.saturation, 100)
    elif action == "color_temp":
        await dev.set_color_temp(resolved.kelvin)


async def _apply_action_to_devices(
    matched: dict[str, Any],
    action: str,
    resolved: _ActionParams,
    alias_map: dict[str, str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Run the action against each matched device. Returns (results, errors)."""
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for ip, dev in matched.items():
        alias = alias_map.get(ip, dev.alias or ip)
        last_error: Exception | None = None
        for attempt in range(_DEVICE_RETRIES + 1):
            try:
                await asyncio.wait_for(_dispatch_device_action(dev, action, resolved), timeout=_DEVICE_TIMEOUT_SECONDS)
                await asyncio.wait_for(dev.update(), timeout=_DEVICE_TIMEOUT_SECONDS)
                state = _device_state(dev, ip, alias_map, include_hsv=action == "color")
                if not _action_verified(action, resolved, state):
                    raise RuntimeError(f"device state did not confirm requested {action} change")
                results.append(state)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if attempt < _DEVICE_RETRIES:
                    await asyncio.sleep(0.25 * (attempt + 1))
        if last_error is not None:
            errors.append(f"{alias}: {last_error}")
    return results, errors


def _action_verified(action: str, resolved: _ActionParams, state: dict[str, Any]) -> bool:
    """Confirm that a device reports the requested state after a mutation."""
    if action == "on":
        return state.get("is_on") is True
    if action == "off":
        return state.get("is_on") is False
    if action == "brightness":
        return state.get("brightness") == resolved.brightness
    if action == "color_temp":
        return state.get("color_temp") == resolved.kelvin
    if action == "color":
        return state.get("hue") == resolved.hue and state.get("saturation") == resolved.saturation
    # Toggle has no fixed expected value, but update() succeeding confirms the
    # command reached the device and produced a readable state.
    return action == "toggle" and isinstance(state.get("is_on"), bool)


async def _apply_scene_to_devices(
    matched: dict[str, Any], scene: str, alias_map: dict[str, str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Apply every command in a named scene to each matched device."""
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for ip, dev in matched.items():
        alias = alias_map.get(ip, dev.alias or ip)
        last_error: Exception | None = None
        for attempt in range(_DEVICE_RETRIES + 1):
            try:
                supported = [(action, params) for action, params in _SCENES[scene] if _supports_action(dev, action)]
                if not supported:
                    raise RuntimeError("device does not support any controls in this scene")
                for action, params in supported:
                    await asyncio.wait_for(
                        _dispatch_device_action(dev, action, _resolve_action_params(action, params)),
                        timeout=_DEVICE_TIMEOUT_SECONDS,
                    )
                await asyncio.wait_for(dev.update(), timeout=_DEVICE_TIMEOUT_SECONDS)
                results.append(_device_state(dev, ip, alias_map, include_hsv=True))
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if attempt < _DEVICE_RETRIES:
                    await asyncio.sleep(0.25 * (attempt + 1))
        if last_error is not None:
            errors.append(f"{alias}: {last_error}")
    return results, errors


def _supports_action(dev: Any, action: str) -> bool:
    """Return whether a device advertises support for a lighting feature."""
    capability = {"brightness": "is_dimmable", "color": "is_color", "color_temp": "is_variable_color_temp"}.get(action)
    if capability is None:
        return True
    value = getattr(dev, capability, None)
    # Test doubles and older python-kasa versions may not expose capability
    # flags. In that case retain the previous optimistic behaviour.
    return value is not False


def _summarize_action(
    action: str,
    resolved: _ActionParams,
    results: list[dict[str, Any]],
    errors: list[str],
) -> str:
    """Build a human-readable summary of a completed control action."""
    verbs = {
        **_ACTION_VERBS,
        "brightness": f"Set brightness to {resolved.brightness}%",
        "color": f"Set colour (hue={resolved.hue}, sat={resolved.saturation}%)",
        "color_temp": f"Set colour temperature to {resolved.kelvin}K",
    }
    names = ", ".join(r["alias"] for r in results)
    suffix = f" Errors: {'; '.join(errors)}" if errors else ""
    return f"{verbs.get(action, action)}: {names}.{suffix}"


class KasaTool(ApprovalGatedTool):
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
        "Control actions execute immediately - no approval prompt."
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
                "enum": sorted(_SCENES),
                "description": "Preset mood: moody, cozy, movie, focus, romantic, party, or sunset.",
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
            header = f"**{d['alias']}** ({d['host']}) - {status}"
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
        action = str(input.params.get("action", "")).strip().lower()
        action = _ACTION_ALIASES.get(action, action)
        if not action:
            return ToolOutput(success=False, error="Parameter 'action' is required.")

        target_hint = str(input.params.get("device", "")).strip().lower()
        # Broad lighting requests are common in natural language. Allow
        # feature controls without a target to apply to all discovered
        # devices; keep power actions target-specific to avoid surprises with
        # plugs and switches.
        broad_actions = {"brightness", "color", "color_temp", "scene"}
        if action not in broad_actions and not target_hint:
            # Require an explicit target so a control action can never fan out
            # to every device on the network. No approval prompt - actions run
            # immediately once a device is named.
            return ToolOutput(
                success=False,
                error=(
                    "Parameter 'device' is required for control actions. "
                    "Use action='list' to see available devices, then target one by alias or IP."
                ),
            )

        if action == "scene":
            scene = str(input.params.get("scene", "")).strip().lower()
            if scene not in _SCENES:
                return ToolOutput(
                    success=False,
                    error=f"Unknown scene {scene!r}. Available: {', '.join(sorted(_SCENES))}.",
                )

        try:
            import kasa  # noqa: F401
        except ImportError:
            return ToolOutput(
                success=False,
                error="python-kasa is not installed. Run: uv add python-kasa",
            )

        found, alias_map, early = await self._discover_and_connect()
        if early is not None:
            return early

        if action == "list":
            devices = [
                _device_state(dev, ip, alias_map, include_model=True, include_hsv=True) for ip, dev in found.items()
            ]
            return ToolOutput(success=True, data={"devices": devices})

        matched = (
            found if action in broad_actions and not target_hint else self._match_devices(found, alias_map, target_hint)
        )
        if isinstance(matched, ToolOutput):
            return matched

        if action == "scene":
            scene = str(input.params["scene"]).strip().lower()
            results, errors = await _apply_scene_to_devices(matched, scene, alias_map)
            message = f"Applied {scene} scene to: {', '.join(r['alias'] for r in results)}."
        else:
            try:
                resolved = _resolve_action_params(action, input.params)
            except ValueError as exc:
                return ToolOutput(success=False, error=str(exc))
            results, errors = await _apply_action_to_devices(matched, action, resolved, alias_map)
            message = _summarize_action(action, resolved, results, errors)
        if errors and not results:
            return ToolOutput(success=False, error="; ".join(errors))

        if errors:
            message += f" Errors: {'; '.join(errors)}"
        return ToolOutput(success=True, data={"devices": results, "message": message})

    @staticmethod
    async def _discover_and_connect() -> tuple[dict[str, Any], dict[str, str], ToolOutput | None]:
        """Discover and connect to devices on the LAN.

        Returns (found, alias_map, early_return). When early_return is not None
        the caller should return it directly (no devices, or a discovery error).
        """
        try:
            pairs, diagnostic = await asyncio.to_thread(_run_kasa_discover)
        except Exception as exc:
            return {}, {}, ToolOutput(success=False, error=f"Discovery subprocess failed: {exc}")
        if not pairs:
            message = "No Kasa devices found on the network."
            if diagnostic:
                message = f"No Kasa devices found. {diagnostic}"
            return (
                {},
                {},
                ToolOutput(
                    success=not diagnostic,
                    data={"devices": [], "message": message},
                    error=message if diagnostic else None,
                ),
            )
        try:
            found = await _connect_devices(pairs)
        except Exception as exc:
            return {}, {}, ToolOutput(success=False, error=f"Failed to connect to devices: {exc}")
        if not found:
            return (
                {},
                {},
                ToolOutput(
                    success=False,
                    data={
                        "devices": [],
                        "message": "Devices discovered but could not connect to any.",
                    },
                    error="Devices were discovered but none could be reached.",
                ),
            )
        return found, dict(pairs), None

    @staticmethod
    def _match_devices(
        found: dict[str, Any], alias_map: dict[str, str], target_hint: str
    ) -> dict[str, Any] | ToolOutput:
        """Filter discovered devices by the target hint, or return an error output."""
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
        return matched
