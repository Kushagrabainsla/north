"""Your Kasa devices, found once and kept connected: the one place north reaches them (CODING_STYLE §7.5).

The kasa tool (what north does for you from chat) and the dashboard's Lights page (what
you do yourself) both come here. Every command used to find the whole network again,
connect, act and disconnect, 2-5 s each: fine for "make the bedroom warm", too slow for
a slider and far too slow to follow music.

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
from dataclasses import dataclass, replace
from typing import Any

from utils.tasks import spawn

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 12.0
# How long a found set of devices is trusted before discovery runs again: a bulb that
# moved to another address, or one plugged in since, is found within this.
_STALE_SECONDS = 300.0


@dataclass(frozen=True)
class LightChange:
    """What to change on a device; None leaves that part as it is."""

    on: bool | None = None
    brightness: int | None = None  # 1-100
    hue: int | None = None  # 0-360
    saturation: int | None = None  # 0-100
    kelvin: int | None = None
    transition_ms: int | None = None  # a fade, honoured by bulbs on the older protocol


# Presets for a mood, shared by the kasa tool and the Lights page.
SCENES: dict[str, LightChange] = {
    "moody": LightChange(brightness=30, kelvin=2700),
    "cozy": LightChange(brightness=35, kelvin=2700),
    "movie": LightChange(brightness=15, kelvin=2500),
    "focus": LightChange(brightness=90, kelvin=5000),
    "romantic": LightChange(brightness=25, hue=330, saturation=75),
    "party": LightChange(brightness=70, hue=300, saturation=100),
    "sunset": LightChange(brightness=40, kelvin=2500),
}


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
        self._finding = asyncio.Lock()
        # The newest change waiting for each device that is being streamed to, and its sender.
        self._latest: dict[str, LightChange] = {}
        self._senders: dict[str, asyncio.Task[None]] = {}
        self.live = LiveLights(self)

    @property
    def problem(self) -> str:
        """Why devices could not be found or reached, or "" when nothing is wrong."""
        return self._problem

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

    def stream(self, device_id: str, change: LightChange) -> None:
        """Send *change* as soon as the device can take it, replacing any change it has not reached yet.

        For a light following music: a bulb that takes 200 ms per request gets the newest frame
        each time, so it never falls further and further behind what is playing.
        """
        self._latest[device_id] = change
        if device_id not in self._senders:
            self._senders[device_id] = spawn(self._send_newest(device_id), name=f"kasa-stream:{device_id}")

    def stop_streams(self) -> None:
        """Drop the changes not sent yet; a request already on its way still lands."""
        self._latest.clear()

    async def _send_newest(self, device_id: str) -> None:
        try:
            while (change := self._latest.pop(device_id, None)) is not None:
                try:
                    await self.apply(device_id, change, confirm=False)
                except Exception:  # noqa: BLE001 - one frame lost is no failure; a silent bulb is not hammered
                    logger.debug("kasa: a streamed change to %s failed", device_id, exc_info=True)
                    await asyncio.sleep(0.5)
        finally:
            self._senders.pop(device_id, None)

    async def aclose(self) -> None:
        """Release every connection: a KLAP device holds an HTTP session until it is closed."""
        await self.live.stop()
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
            for host, name in pairs:
                if host in known:
                    self._names[known[host]] = name
                    continue
                try:
                    dev = await asyncio.wait_for(self._open(host), _TIMEOUT_SECONDS)
                    await asyncio.wait_for(dev.update(), _TIMEOUT_SECONDS)
                except Exception as exc:  # noqa: BLE001 - one bulb that will not answer must not hide the rest
                    errors.append(f"{name} ({host}): {type(exc).__name__}: {exc}")
                    continue
                device_id = str(getattr(dev, "device_id", "") or host)
                if (moved := self._devices.get(device_id)) is not None:
                    await close_devices([moved])
                self._devices[device_id] = dev
                self._names[device_id] = name
            self._found_at = time.monotonic()
            self._problem = _why_not(pairs, diagnostic, errors, bool(self._devices))

    async def _read(self, device_id: str) -> None:
        await asyncio.wait_for(self._devices[device_id].update(), _TIMEOUT_SECONDS)

    async def _reconnect(self, device_id: str) -> Any:
        stale = self._devices[device_id]
        await close_devices([stale])
        dev = await asyncio.wait_for(self._open(stale.host), _TIMEOUT_SECONDS)
        await asyncio.wait_for(dev.update(), _TIMEOUT_SECONDS)
        self._devices[device_id] = dev
        return dev


class LiveLights:
    """Lights following something live, such as music, then put back as they were.

    Started from the dashboard, which sends frames from the music it hears; north only sends
    them on. If frames stop arriving (the tab was closed) the lights are put back by themselves.
    """

    def __init__(self, devices: KasaDevices, idle_seconds: float = 5.0) -> None:
        self._devices = devices
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
        found = {d["id"]: d for d in await self._devices.devices() if not d.get("unreachable")}
        self._before = {i: _as_change(found[i]) for i in device_ids if i in found}
        self._last_frame = time.monotonic()
        self._watch = spawn(self._stop_when_idle(), name="kasa-live-watch")
        logger.info("Lights: music sync started on %s", list(self._before))
        return self.following

    def frame(self, changes: dict[str, LightChange]) -> None:
        """Send each following light its newest change; any other light is left alone."""
        if not self._before:
            raise LookupError("No lights are following anything: start first.")
        self._last_frame = time.monotonic()
        for device_id, change in changes.items():
            if device_id in self._before:
                self._devices.stream(device_id, change)

    async def stop(self) -> None:
        """Stop following, and put every light back as it was."""
        if self._watch is not None and self._watch is not asyncio.current_task():
            self._watch.cancel()
        self._watch = None
        before, self._before = self._before, {}
        if not before:
            return
        self._devices.stop_streams()
        await asyncio.gather(
            *(self._devices.apply(i, change, confirm=False) for i, change in before.items()), return_exceptions=True
        )
        logger.info("Lights: music sync stopped; %s put back", list(before))

    async def _stop_when_idle(self) -> None:
        while self._before:
            await asyncio.sleep(min(1.0, self._idle_seconds / 2))
            if time.monotonic() - self._last_frame > self._idle_seconds:
                logger.info("Lights: no music frames for %.0f s; putting the lights back", self._idle_seconds)
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


def describe(device_id: str, dev: Any, name: str | None = None) -> dict[str, Any]:
    """A device as the tool and the Lights page show it: what it is, what it can do, its state."""
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


def _why_not(pairs: list[tuple[str, str]], diagnostic: str, errors: list[str], reached: bool) -> str:
    if not pairs:
        return diagnostic
    if not errors:
        return ""
    why = "; ".join(errors)
    if "AuthenticationError" in why and not credential_args():
        from config.settings import settings

        why += (
            " No TP-Link account is configured: set NORTH_KASA_USERNAME and NORTH_KASA_PASSWORD in "
            f"{settings.north_home / '.env'}. Kasa firmware since 2023 requires one."
        )
    return why if not reached else f"Some devices could not be reached: {why}"


def credential_args() -> list[str]:
    """The TP-Link login, when one is configured. Empty otherwise.

    Devices speaking KLAP - most firmware since 2023 - answer discovery without a
    credential and then refuse everything else, so this is the difference between
    seeing a bulb and being able to switch it on.
    """
    from config.settings import settings

    if not (settings.kasa_username and settings.kasa_password):
        return []
    return ["--username", settings.kasa_username, "--password", settings.kasa_password]


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
    """Open one device, letting discovery work out how to talk to it.

    ``Device.connect(host=...)`` assumes the legacy protocol and dials port 9999,
    which a KLAP device is not listening on - so every bulb failed with "connect
    call failed" no matter what credentials were set. ``discover_single`` asks the
    device which protocol and port it speaks before connecting, and carries the
    login while it does.

    The credentials have to travel here as well as to the discovery subprocess:
    discovery only has to *see* a device, control has to authenticate to it.
    """
    from kasa import Discover

    from config.settings import settings

    return await Discover.discover_single(
        host,
        timeout=int(_TIMEOUT_SECONDS),
        username=settings.kasa_username or None,
        password=settings.kasa_password or None,
    )


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
