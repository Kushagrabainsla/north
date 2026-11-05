"""Your Kasa devices, found once and kept connected: the one place north reaches them (CODING_STYLE §7.5).

The kasa tool (what north does for you from chat) and the dashboard's Devices page (what
you do yourself, through `tools/home.py`, which also streams music sync) both come here.
Every command used to find the whole network again, connect, act and disconnect, 2-5 s
each: fine for "make the bedroom warm", too slow for a slider and far too slow to follow
music.

Discovery still runs `kasa --json discover` in a subprocess, because UDP broadcast
discovery conflicts with the server's own event loop on macOS. It runs when the devices
are first needed, when you ask to look again, and once they are `_STALE_SECONDS` old;
each device it finds stays connected until north stops.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from tools.home import LightChange

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 12.0
# How long a found set of devices is trusted before discovery runs again: a bulb that
# moved to another address, or one plugged in since, is found within this.
_STALE_SECONDS = 300.0


class KasaDevices:
    """The Kasa devices on your network, kept connected, and the one way to change them."""

    def __init__(
        self,
        discover: Callable[[], tuple[list[tuple[str, str]], str]] | None = None,
        open_device: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        self._discover = discover or run_discovery
        self._open = open_device or open_device_at
        self._devices: dict[str, Any] = {}  # device id -> a connected python-kasa Device
        self._names: dict[str, str] = {}  # device id -> the name discovery reported
        self._found_at: float | None = None
        self._problem = ""
        self._locked: list[str] = []  # devices found that refused every TP-Link login north has
        self._finding = asyncio.Lock()

    @property
    def problem(self) -> str:
        """Why devices could not be found or reached, or "" when nothing is wrong."""
        return self._problem

    @property
    def locked(self) -> list[str]:
        """The devices found that refused every TP-Link login north has, by name and address."""
        return list(self._locked)

    async def devices(self, *, look_again: bool = False) -> list[dict[str, Any]]:
        """Every device found, with what it can do and its state now."""
        await self._find(look_again)
        ids = list(self._devices)
        reached = await asyncio.gather(*(self._read(device_id) for device_id in ids), return_exceptions=True)
        return [
            describe(device_id, self._devices[device_id], self._names.get(device_id))
            | ({"unreachable": f"{type(error).__name__}: {error}"} if isinstance(error, BaseException) else {})
            for device_id, error in zip(ids, reached, strict=True)
        ]

    async def apply(self, device_id: str, change: LightChange, *, confirm: bool = True) -> dict[str, Any]:
        """Make *change* on one device and return its state.

        Only what the device can do is sent, a white temperature within its own range.
        With *confirm* the device is read back, and a state it does not report raises:
        "done" is what the device says, not what was asked. A dropped connection is
        opened again once before giving up.
        """
        await self._find(False)
        if device_id not in self._devices:
            raise LookupError(f"No device {device_id!r} on this network now.")
        dev = self._devices[device_id]
        sent = fit(dev, change)
        try:
            await asyncio.wait_for(_send(dev, sent), _TIMEOUT_SECONDS)
        except (TimeoutError, OSError, ConnectionError) as first:
            logger.info("kasa: %s did not answer (%s); connecting again", device_id, first)
            dev = await self._reconnect(device_id)
            await asyncio.wait_for(_send(dev, sent), _TIMEOUT_SECONDS)
        if not confirm:
            return describe(device_id, dev, self._names.get(device_id))
        await asyncio.wait_for(dev.update(), _TIMEOUT_SECONDS)
        state = describe(device_id, dev, self._names.get(device_id))
        if not _reports(state, sent):
            raise RuntimeError(f"{state['name']} did not confirm the change")
        return state

    async def aclose(self) -> None:
        """Release every connection: a KLAP device holds an HTTP session until it is closed."""
        await close_devices(self._devices.values())
        self._devices.clear()
        self._found_at = None

    async def _find(self, look_again: bool) -> None:
        async with self._finding:
            fresh = self._found_at is not None and time.monotonic() - self._found_at < _STALE_SECONDS
            if fresh and not look_again:
                return
            pairs, diagnostic = await asyncio.to_thread(self._discover)
            known = {getattr(dev, "host", ""): device_id for device_id, dev in self._devices.items()}
            errors: list[str] = []
            locked: list[str] = []
            for host, name in pairs:
                if host in known:
                    self._names[known[host]] = name
                    continue
                try:
                    # Each saved account may be tried in turn, each with its own timeout.
                    dev = await asyncio.wait_for(self._open(host), _TIMEOUT_SECONDS * 3)
                    await asyncio.wait_for(dev.update(), _TIMEOUT_SECONDS)
                except Exception as exc:  # noqa: BLE001 - one bulb that will not answer must not hide the rest
                    if _refused_login(exc):
                        locked.append(f"{name} ({host})")
                    else:
                        errors.append(f"{name} ({host}): {type(exc).__name__}: {exc}")
                    continue
                device_id = str(getattr(dev, "device_id", "") or host)
                if (moved := self._devices.get(device_id)) is not None:
                    await close_devices([moved])
                self._devices[device_id] = dev
                self._names[device_id] = name
            self._found_at = time.monotonic()
            self._locked = locked
            self._problem = _why_not(pairs, diagnostic, errors, locked)

    async def _read(self, device_id: str) -> None:
        await asyncio.wait_for(self._devices[device_id].update(), _TIMEOUT_SECONDS)

    async def _reconnect(self, device_id: str) -> Any:
        stale = self._devices[device_id]
        await close_devices([stale])
        dev = await asyncio.wait_for(self._open(stale.host), _TIMEOUT_SECONDS)
        await asyncio.wait_for(dev.update(), _TIMEOUT_SECONDS)
        self._devices[device_id] = dev
        return dev


def describe(device_id: str, dev: Any, name: str | None = None) -> dict[str, Any]:
    """A device as the tool and the Devices page show it: what it is, what it can do, its state."""
    entry: dict[str, Any] = {
        "id": device_id,
        "name": getattr(dev, "alias", None) or name or getattr(dev, "host", device_id),
        "host": getattr(dev, "host", ""),
        "model": getattr(dev, "model", ""),
        "on": bool(getattr(dev, "is_on", False)),
    }
    light = _light(dev)
    if light is None:
        return entry | {"kind": "switch", "can": {"dim": False, "color": False, "white": None}}
    white = light.get_feature("color_temp") if light.has_feature("color_temp") else None
    can = {
        "dim": light.has_feature("brightness"),
        "color": light.has_feature("hsv"),
        "white": [white.minimum_value, white.maximum_value] if white is not None else None,
    }
    entry |= {"kind": "light", "can": can}
    if can["dim"]:
        entry["brightness"] = light.brightness
    kelvin = light.color_temp if can["white"] else 0
    if kelvin:
        entry["kelvin"] = kelvin
    elif can["color"]:
        entry["hue"], entry["saturation"] = light.hsv.hue, light.hsv.saturation
    return entry


def fit(dev: Any, change: LightChange) -> LightChange:
    """*change* as far as the device can carry it out.

    Raises ValueError when it can do none of it: a scene of colours sent to a plug.
    """
    light = _light(dev)
    if light is None:
        if change.on is None and any(v is not None for v in (change.brightness, change.hue, change.kelvin)):
            raise ValueError("this device can only be switched on and off")
        return LightChange(on=change.on)
    brightness = change.brightness if light.has_feature("brightness") else None
    hue, saturation = (change.hue, change.saturation) if light.has_feature("hsv") else (None, None)
    kelvin = None
    if change.kelvin is not None and light.has_feature("color_temp"):
        white = light.get_feature("color_temp")
        kelvin = max(white.minimum_value, min(white.maximum_value, change.kelvin))
    fitted = replace(change, brightness=brightness, hue=hue, saturation=saturation, kelvin=kelvin)
    asked = any(v is not None for v in (change.brightness, change.hue, change.kelvin))
    if asked and change.on is None and all(v is None for v in (brightness, hue, kelvin)):
        raise ValueError("this device does not support any of that")
    return fitted


async def _send(dev: Any, change: LightChange) -> None:
    light = _light(dev)
    if change.on is False:
        await dev.turn_off()
        return
    if light is None:
        await dev.turn_on()
        return
    from kasa.interfaces.light import LightState
    from kasa.iot import IotDevice

    # A colour is only shown once the bulb leaves white mode, which colour temperature 0 means.
    white = change.kelvin if change.kelvin is not None else (0 if change.hue is not None else None)
    state = LightState(
        light_on=True,
        brightness=change.brightness,
        hue=change.hue,
        saturation=change.saturation if change.hue is not None else None,
        color_temp=white if light.has_feature("color_temp") else None,
        # Only bulbs on the older protocol fade; a KLAP bulb would be sent a field it does not know.
        transition=change.transition_ms if isinstance(dev, IotDevice) else None,
    )
    await light.set_state(state)


def _reports(state: dict[str, Any], sent: LightChange) -> bool:
    """Whether the device reads back what was sent to it."""
    if sent.on is not None and state["on"] is not sent.on:
        return False
    if sent.on is False:
        return True
    checks = (
        ("brightness", sent.brightness),
        ("kelvin", sent.kelvin),
        ("hue", sent.hue),
        ("saturation", sent.saturation),
    )
    return all(state.get(key) == value for key, value in checks if value is not None)


def _light(dev: Any) -> Any:
    from kasa import Module

    return getattr(dev, "modules", {}).get(Module.Light)


def _why_not(pairs: list[tuple[str, str]], diagnostic: str, errors: list[str], locked: list[str]) -> str:
    """What stops north from controlling the devices it found, or "" when nothing does.

    Every bulb that refused the login was named with python-kasa's long error, four times over,
    followed by a hint to check the Wi-Fi, though the bulbs had been found.
    """
    if not pairs:
        return diagnostic
    parts = []
    if locked:
        one = len(locked) == 1
        count = f"{len(locked)} bulb{'' if one else 's'}"
        where = "Settings > Connections > Kasa lights (or NORTH_KASA_USERNAME and NORTH_KASA_PASSWORD in north's .env)"
        if accounts():
            parts.append(
                f"{count} refused the TP-Link logins north has: {', '.join(locked)}. Each bulb answers only the "
                f"account it was set up with in the Kasa app; add that one in {where}."
            )
        else:
            parts.append(
                f"{count} {'needs' if one else 'need'} the TP-Link login {'it was' if one else 'they were'} set up "
                f"with in the Kasa app: {', '.join(locked)}. "
                f"Add it in {where}."
            )
    if errors:
        parts.append(f"Not answering: {'; '.join(errors)}")
    return " ".join(parts)


def _refused_login(error: BaseException) -> bool:
    from kasa.exceptions import AuthenticationError

    return isinstance(error, AuthenticationError) or type(error).__name__ == "AuthenticationError"


def accounts() -> list[tuple[str, str]]:
    """The TP-Link logins north has, in the order they are tried: yours, then a second one."""
    from config.settings import settings

    logins = [
        (settings.kasa_username, settings.kasa_password),
        (settings.kasa_second_username, settings.kasa_second_password),
    ]
    return [(username, password) for username, password in logins if username and password]


def credential_args() -> list[str]:
    """The TP-Link login, when one is configured. Empty otherwise.

    Devices speaking KLAP - most firmware since 2023 - answer discovery without a
    credential and then refuse everything else, so this is the difference between
    seeing a bulb and being able to switch it on.
    """
    if not (logins := accounts()):
        return []
    username, password = logins[0]
    return ["--username", username, "--password", password]


def parse_discovery(payload: str) -> list[tuple[str, str]]:
    """Read `kasa --json discover` into [(ip, alias), ...].

    JSON rather than the CLI's prose, because the prose changed underneath us:
    the scraper looked for "Host: 10.0.0.36" and python-kasa 0.10 prints
    "IP:  10.0.0.36", so every device on the network parsed as no devices at all.

    A device that has not been authenticated reports no alias, so the address
    stands in - a bulb the user cannot name is still a bulb worth listing.
    """
    try:
        found = json.loads(payload)
    except json.JSONDecodeError:
        return []
    if not isinstance(found, dict):
        return []
    pairs: list[tuple[str, str]] = []
    for host, record in found.items():
        info = record if isinstance(record, dict) else {}
        system = info.get("system") if isinstance(info.get("system"), dict) else {}
        inner = system.get("get_sysinfo") if isinstance(system.get("get_sysinfo"), dict) else {}
        alias = info.get("alias") or inner.get("alias") or info.get("device_model") or host
        pairs.append((str(info.get("ip") or host), str(alias)))
    return pairs


def authentication_failed(payload: str) -> int:
    """How many discovered devices refused the credentials north offered.

    Counted so the caller can say *why* nothing is controllable. This was the
    failure that reached the user as silence: two bulbs on the network, both
    reporting "Authentication failed", and a tool that reported no devices.
    """
    return payload.count("Authentication failed")


def run_discovery() -> tuple[list[tuple[str, str]], str]:
    """Run `kasa --json discover` as a subprocess. Returns (pairs, diagnostic).

    *pairs* is [(ip, alias), ...]. *diagnostic* is an empty string on success, or
    a human-readable reason when discovery produced nothing - a missing binary, a
    timeout, or devices that would not authenticate - so the caller can surface it
    instead of a silent "no devices found".
    """
    kasa_bin = shutil.which("kasa") or f"{sys.executable.rsplit('/', 1)[0]}/kasa"
    credentials = credential_args()
    last_err = ""
    for attempt in ("binary", "module"):
        base = [kasa_bin] if attempt == "binary" else [sys.executable, "-m", "kasa"]
        cmd = [*base, "--json", *credentials, "discover"]
        try:
            # North is commonly launched detached, where stdin may be closed.
            # Explicitly detach all standard input so Python-based CLIs cannot
            # fail during interpreter startup with EBADF (Errno 9).
            result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
            output = result.stdout
            last_err = (result.stderr or "").strip()
            if result.returncode != 0:
                # A broken/missing binary should not prevent the module fallback.
                # Preserve the diagnostic if both discovery methods fail.
                last_err = f"`{' '.join(redacted(cmd))}` exited {result.returncode}: {last_err or 'no output'}"
                continue
        except FileNotFoundError:
            last_err = f"kasa executable not found at {kasa_bin}"
            continue  # try the `-m kasa` fallback
        except subprocess.TimeoutExpired:
            return [], "kasa discover timed out (UDP broadcast did not complete in 20s - check network/firewall)"
        except Exception as exc:  # noqa: BLE001
            return [], f"kasa discover raised: {exc}"

        pairs = parse_discovery(output)
        if pairs:
            return pairs, ""
        # Nothing parsed. Say which of the two reasons it was, rather than
        # returning an empty list and an empty explanation.
        refused = authentication_failed(result.stderr or "") + authentication_failed(output)
        if refused:
            from config.settings import settings

            return [], (
                f"{refused} device(s) refused authentication. Newer Kasa firmware needs a TP-Link "
                f"account: set NORTH_KASA_USERNAME and NORTH_KASA_PASSWORD in {settings.north_home / '.env'}."
            )
        return [], last_err or "kasa discover found nothing on this network"

    return [], last_err or "kasa discovery produced no output"


def redacted(cmd: list[str]) -> list[str]:
    """The command with the password blanked, for logs and error messages."""
    out = list(cmd)
    for index, token in enumerate(out):
        if token == "--password" and index + 1 < len(out):
            out[index + 1] = "***"
    return out


async def open_device_at(host: str) -> Any:
    """Open one device and read it, with the first TP-Link login it accepts.

    ``Device.connect(host=...)`` assumes the legacy protocol and dials port 9999,
    which a KLAP device is not listening on - so every bulb failed with "connect
    call failed" no matter what credentials were set. ``discover_single`` asks the
    device which protocol and port it speaks before connecting, and carries the
    login while it does.

    A bulb answers only the account it was set up with in the Kasa app, so each saved
    login is tried in turn: yours, then a friend's. With none saved, python-kasa tries
    the defaults that a bulb never linked to an account accepts.
    """
    from kasa import Discover

    refused: BaseException = LookupError(f"no TP-Link login was tried on {host}")
    for username, password in accounts() or [("", "")]:
        dev = None
        try:
            dev = await Discover.discover_single(
                host, timeout=int(_TIMEOUT_SECONDS), username=username or None, password=password or None
            )
            if dev is None:
                raise LookupError(f"nothing answered at {host}")
            await dev.update()
            return dev
        except Exception as exc:
            if dev is not None:
                await close_devices([dev])
            if not _refused_login(exc):
                raise
            refused = exc
    raise refused


async def close_devices(devices: Any) -> None:
    """Release the HTTP sessions opened for *devices*.

    A KLAP device is reached over aiohttp, and each open device holds a session
    until it is disconnected. Nothing did, so every command leaked one connector
    per device - invisible in a CLI run and steady growth in a server that has
    been up for days.
    """
    for dev in list(devices):
        disconnect = getattr(dev, "disconnect", None)
        if disconnect is None:
            continue
        try:
            await disconnect()
        except Exception:  # noqa: BLE001 - closing must never fail a command
            logger.debug("kasa: failed to disconnect a device", exc_info=True)
