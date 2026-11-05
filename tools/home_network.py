"""Every device on your home network, named as far as north can tell: the Devices page's full list.

The probes in `home_discovery` hear the devices that announce themselves. Many do not: a smart plug
that talks only to its maker's cloud still has an address on your network. This looks at all of
them. It wakes each address around this computer's (the 254 of a /24 network, as most homes use),
reads which answered from the computer's table of the addresses it has heard (ARP), and names each
one by what it can: the maker of its network card (the IEEE registry, kept in
`resources/network-makers.json.gz`), the name your router gives it, a web page it serves and that
page's title, the services it announces, and a few ports smart devices listen on. Nothing here
changes a device; north only asks.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import gzip
import html
import ipaddress
import json
import logging
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from tools.home_discovery import Found, Heard, Scan, discover

logger = logging.getLogger(__name__)

_MAKERS = Path(__file__).resolve().parents[1] / "resources" / "network-makers.json.gz"
# How long the table of addresses has to fill after every address is woken.
_SETTLE_SECONDS = 1.2
_CONNECT_SECONDS = 0.8
_NAME_SECONDS = 2.5
# Ports that say what a device is, in plain words; one that answers is listed with what it means.
PORTS: dict[int, str] = {
    80: "a web page",
    8080: "a web page",
    443: "a secure web page",
    6668: "Tuya's local control port (Smart Life and similar apps)",
    9999: "TP-Link Kasa's local control port",
    8008: "Google Cast",
    1400: "Sonos",
    1883: "MQTT, a messaging service smart devices use",
    22: "SSH, as a computer or hub has",
    631: "a printer",
}
_WEB_PORTS = (80, 8080)
# Services a device announces, in plain words; one not listed here is shown as it is.
SERVICES: dict[str, str] = {
    "_googlecast._tcp": "Google Cast",
    "_airplay._tcp": "AirPlay",
    "_raop._tcp": "AirPlay audio",
    "_companion-link._tcp": "Apple's link between your own devices",
    "_spotify-connect._tcp": "Spotify Connect",
    "_sonos._tcp": "Sonos",
    "_hap._tcp": "HomeKit",
    "_matter._tcp": "Matter",
    "_matterc._udp": "Matter, waiting to be set up",
    "_hue._tcp": "Philips Hue",
    "_home-assistant._tcp": "Home Assistant",
    "_esphomelib._tcp": "ESPHome",
    "_shelly._tcp": "Shelly",
    "_elg._tcp": "Elgato",
    "_wled._tcp": "WLED",
    "_nanoleafapi._tcp": "Nanoleaf",
    "_amzn-wplay._tcp": "Amazon Fire TV",
    "_androidtvremote2._tcp": "Android TV",
    "_ipp._tcp": "a printer",
    "_printer._tcp": "a printer",
    "_smb._tcp": "file sharing",
    "_ssh._tcp": "SSH",
    "_http._tcp": "a web page",
}
# Makers whose network cards are in smart plugs, bulbs and switches: listed before laptops and TVs.
SMART_HOME_MAKERS = frozenset(
    {
        "Espressif",
        "Tuya",
        "TP-Link",
        "Shelly",
        "Philips Hue (Signify)",
        "LIFX",
        "WiZ",
        "Govee",
        "Yeelight",
        "Nanoleaf",
        "Meross",
        "BroadLink",
        "Belkin",
        "Xiaomi",
        "Wyze",
        "ecobee",
        "Resideo (Honeywell Home)",
        "Honeywell",
        "Lutron",
        "Leviton",
        "Google Nest",
        "Ring",
        "Amazon",
    }
)
# A hardware address the device made up, as phones, tablets and laptops do to stay private.
PRIVATE = "a private address"
# What a maker's name alone does not say.
HINTS = {
    "Espressif": "Espressif makes the chip in many smart plugs, bulbs and switches of many brands",
    "Tuya": "Tuya makes the insides of many plugs and bulbs sold under other names, run from Smart Life",
    PRIVATE: "phones, tablets and laptops make up their hardware address to stay private",
}


@dataclass
class NetworkDevice:
    """One device on your network, with everything north could tell about it."""

    address: str
    mac: str = ""
    maker: str = ""
    names: list[str] = field(default_factory=list)
    offers: list[str] = field(default_factory=list)
    role: str = ""  # "this computer" or "your router": listed, never offered to connect
    found: Found | None = None  # a brand north recognises on it
    here: str = ""  # "kasa", or the name of north's tool for it: already on the Devices page

    @property
    def connect(self) -> str:
        """What the page offers: "add" a brand north knows the way into, "try" anything else, or nothing."""
        if self.role:
            return "none"
        if self.here:
            return "here"
        return "add" if self.found is not None and not self.found.brand.cannot else "try"

    def name(self) -> str:
        if self.role == "this computer":
            return "This computer, where North runs"
        if self.names:
            return self.names[0]
        if self.role:
            return self.role.capitalize()
        if self.found is not None:
            return self.found.name
        if self.maker == PRIVATE:
            return "a phone, tablet or computer"
        return f"a {self.maker} device" if self.maker else "an unnamed device"

    def shown(self) -> dict[str, Any]:
        return {
            "id": self.address,
            "address": self.address,
            "mac": self.mac,
            "maker": self.maker,
            "hint": HINTS.get(self.maker, ""),
            "name": self.name(),
            "names": self.names,
            "offers": self.offers,
            "role": self.role,
            "found": self.found.shown() if self.found is not None else None,
            "here": self.here,
            "connect": self.connect,
        }

    def order(self) -> tuple[int, int, str]:
        rank = {"add": 0, "here": 1, "try": 2, "none": 4}[self.connect]
        if rank == 2 and self.maker not in SMART_HOME_MAKERS and self.found is None:
            rank = 3  # a laptop or a TV after anything that might be a light or plug
        return rank, 0 if self.names else 1, ipaddress.ip_address(self.address).packed.hex()


class LocalNetwork:
    """This computer's view of the network. Tests give `survey` another with the same methods."""

    def own_address(self) -> str:
        """This computer's address on the network its default route uses; no packet is sent."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # a documentation address: only chooses the interface
            return sock.getsockname()[0]

    def router(self) -> str:
        return default_route()

    def wake(self, hosts: list[str]) -> None:
        """Send each host one empty UDP packet, so the computer asks who has its address (ARP)."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            for host in hosts:
                with contextlib.suppress(OSError):
                    sock.sendto(b"", (host, 9))

    def neighbours(self) -> dict[str, str]:
        return neighbour_table()

    async def answers(self, host: str, port: int) -> bool:
        try:
            _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), _CONNECT_SECONDS)
        except (OSError, TimeoutError):
            return False
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True

    async def page(self, host: str, port: int) -> str:
        """The title of the web page *host* serves on *port*, or its server's name; "" when neither."""
        url = f"http://{host}:{port}/" if port != 80 else f"http://{host}/"
        # trust_env=False: a proxy set for the internet must never carry a request to a device at home.
        async with httpx.AsyncClient(timeout=2.0, follow_redirects=True, trust_env=False) as client:
            response = await client.get(url, headers={"User-Agent": "north"})
        title = re.search(r"<title[^>]*>(.*?)</title>", response.text[:65536], re.I | re.S)
        text = html.unescape(title.group(1)).strip() if title else ""
        return " ".join(text.split())[:80] or response.headers.get("server", "")[:80]

    def hostname(self, host: str) -> str:
        try:
            name = socket.gethostbyaddr(host)[0]
        except OSError:
            return ""
        return "" if name == host else name


async def look(network: LocalNetwork | None = None) -> list[NetworkDevice]:
    """Every device on the network: what announced itself and what only answered, named as far as north can."""
    network = network or LocalNetwork()
    scan, table = await asyncio.gather(discover(), asyncio.to_thread(_sweep, network))
    return await survey(scan, table, network)


def _sweep(network: LocalNetwork) -> tuple[str, str, dict[str, str]]:
    import time

    own = network.own_address()
    hosts = [str(ip) for ip in ipaddress.ip_network(f"{own}/24", strict=False).hosts() if str(ip) != own]
    network.wake(hosts)
    time.sleep(_SETTLE_SECONDS)
    subnet = ipaddress.ip_network(f"{own}/24", strict=False)
    table = {ip: mac for ip, mac in network.neighbours().items() if ipaddress.ip_address(ip) in subnet}
    return own, network.router(), table


async def survey(
    scan: Scan, table: tuple[str, str, dict[str, str]], network: LocalNetwork | None = None
) -> list[NetworkDevice]:
    """One entry per address, from what announced itself and what answered, with what each offers."""
    network = network or LocalNetwork()
    own, router, macs = table
    devices: dict[str, NetworkDevice] = {}

    def device(address: str) -> NetworkDevice:
        return devices.setdefault(address, NetworkDevice(address))

    for address, mac in macs.items():
        device(address).mac = mac
    for address, said in scan.heard.items():
        _describe(device(address), said)
    for item in scan.found:
        entry = device(item.host)
        if entry.found is None:
            entry.found = item
    if own:
        device(own).role = "this computer"
    if router and router in devices:
        devices[router].role = "your router"
    await _probe(list(devices.values()), network)
    for entry in devices.values():
        entry.maker = entry.maker or maker(entry.mac)
        entry.names = list(dict.fromkeys(entry.names))
        entry.offers = list(dict.fromkeys(entry.offers))
    return sorted(devices.values(), key=NetworkDevice.order)


def _describe(entry: NetworkDevice, said: Heard) -> None:
    entry.names += sorted(said.names)
    entry.offers += [SERVICES.get(service, service) for service in sorted(said.services)]
    if said.server:
        entry.offers.append(f"a UPnP service ({said.server})")


async def _probe(devices: list[NetworkDevice], network: LocalNetwork) -> None:
    """Which of `PORTS` each device answers on, its web page's title, and its name on the network."""
    limit = asyncio.Semaphore(96)
    candidates = [entry for entry in devices if entry.role != "this computer"]

    async def ports(entry: NetworkDevice) -> None:
        async def one(port: int) -> int | None:
            async with limit:
                return port if await network.answers(entry.address, port) else None

        open_ports = [port for port in await asyncio.gather(*(one(p) for p in PORTS)) if port is not None]
        for port in open_ports:
            entry.offers.append(PORTS[port])
        if web := next((port for port in _WEB_PORTS if port in open_ports), None):
            with contextlib.suppress(Exception):
                if title := await network.page(entry.address, web):
                    entry.offers = [o for o in entry.offers if o != "a web page"] + [f'a web page, "{title}"']

    names = {
        entry.address: asyncio.create_task(asyncio.to_thread(network.hostname, entry.address)) for entry in candidates
    }
    await asyncio.gather(*(ports(entry) for entry in candidates))
    if names:
        await asyncio.wait(names.values(), timeout=_NAME_SECONDS)
    for entry in candidates:
        task = names[entry.address]
        if task.done() and not task.exception() and (name := _plain_hostname(task.result())):
            entry.names.append(name)


def _plain_hostname(name: str) -> str:
    """A router's name for a device without the home domain: "Living-Room-TV" from "Living-Room-TV.lan"."""
    first = name.split(".", 1)[0]
    return "" if not first or first.lower() in {"localhost", "unknown", "_gateway"} else first


# ── The network's own tables ─────────────────────────────────────────────────


_ARP_LINE = re.compile(r"\((\d+\.\d+\.\d+\.\d+)\) at ([0-9a-fA-F:]+)")


def neighbour_table() -> dict[str, str]:
    """IPv4 address -> hardware address of every device this computer has heard (its ARP table)."""
    proc = Path("/proc/net/arp")
    if proc.exists():
        return parse_proc_arp(proc.read_text(encoding="utf-8"))
    arp = shutil.which("arp") or "/usr/sbin/arp"
    try:
        listed = subprocess.run([arp, "-an"], capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_arp(listed)


def parse_proc_arp(text: str) -> dict[str, str]:
    table: dict[str, str] = {}
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4 and parts[2] != "0x0":  # 0x0: asked, never answered
            table[parts[0]] = _mac(parts[3])
    return {ip: mac for ip, mac in table.items() if mac}


def parse_arp(text: str) -> dict[str, str]:
    """`arp -an` as macOS and BSD print it: "? (10.0.0.1) at 0:11:22:aa:b:c on en0 ifscope [ethernet]"."""
    table = {ip: _mac(mac) for ip, mac in _ARP_LINE.findall(text)}
    return {ip: mac for ip, mac in table.items() if mac}


def _mac(text: str) -> str:
    parts = text.split(":")
    if len(parts) != 6:
        return ""
    mac = ":".join(part.zfill(2).lower() for part in parts)
    return "" if mac in {"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"} else mac


def default_route() -> str:
    """The router's address: the gateway of the default route; "" when it cannot be read."""
    proc = Path("/proc/net/route")
    if proc.exists():
        return parse_proc_route(proc.read_text(encoding="utf-8"))
    if sys.platform == "darwin":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            out = subprocess.run(["/sbin/route", "-n", "get", "default"], capture_output=True, text=True, timeout=5)
            if match := re.search(r"gateway:\s*(\d+\.\d+\.\d+\.\d+)", out.stdout):
                return match.group(1)
    return ""


def parse_proc_route(text: str) -> str:
    """The default route's gateway from Linux's /proc/net/route, whose addresses are little-endian hex."""
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) > 2 and parts[1] == "00000000":
            return socket.inet_ntoa(bytes.fromhex(parts[2])[::-1])
    return ""


@functools.cache
def _makers() -> dict[str, str]:
    try:
        return json.loads(gzip.decompress(_MAKERS.read_bytes()))
    except (OSError, ValueError):
        logger.warning("home network: the list of network-card makers could not be read from %s", _MAKERS)
        return {}


def maker(mac: str) -> str:
    """Who made the network card with this hardware address; a private one says what it most likely is."""
    if not mac:
        return ""
    first = int(mac[:2], 16)
    if first & 0b10:  # locally administered: a phone, tablet or laptop hiding its real address
        return PRIVATE
    return _makers().get(mac.replace(":", "")[:6].upper(), "")
