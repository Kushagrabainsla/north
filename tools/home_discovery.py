"""What is on your network that north could control: the Devices page's Find devices.

Listens for the ways lights, plugs and switches announce themselves on a home network,
with the standard library only: mDNS (Hue, Shelly, Elgato, Nanoleaf, WLED, ESPHome, and
HomeKit and Matter devices), SSDP (Hue, WeMo), and each brand's own call (WiZ, Yeelight,
Govee, LIFX). Each thing found says how north would reach it, or why it cannot yet, so
the page offers to add only what north can connect to. Kasa is not looked for here: north
finds and controls Kasa devices itself.

Every probe is a blocking UDP socket with a deadline, run in a thread: one that cannot
open (a port in use, no multicast route) finds nothing and the others still answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import struct
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

LISTEN_SECONDS = 3.0


@dataclass(frozen=True)
class Brand:
    """A kind of device north knows how to recognise, and how it would reach it."""

    name: str
    key: str  # names the tool north writes for it: "hue" for hue_lights
    kind: str
    reach: str  # how north would control it, said to the user and to north
    cannot: str = ""  # why north cannot connect to it yet; "" when it can


HUE = Brand("Philips Hue", "hue", "bridge", "the bridge's local API; pressing its link button once gives north a key")
SHELLY = Brand("Shelly", "shelly", "device", "its local HTTP API, with no login unless one was set")
ELGATO = Brand("Elgato", "elgato", "light", "its local HTTP API on port 9123, with no login")
NANOLEAF = Brand("Nanoleaf", "nanoleaf", "light panels", "its local API; holding its power button 5 s lets north pair")
WLED = Brand("WLED", "wled", "light strip", "its local JSON API, with no login")
ESPHOME = Brand("ESPHome", "esphome", "device", "its web server API, if it is turned on in the device's configuration")
WEMO = Brand("Belkin WeMo", "wemo", "plug", "its local UPnP API, with no login")
WIZ = Brand("WiZ", "wiz", "light", "its local UDP API on port 38899, with no login")
YEELIGHT = Brand("Yeelight", "yeelight", "light", "its local API on port 55443; LAN Control must be on in its app")
GOVEE = Brand("Govee", "govee", "light", "its LAN API; LAN Control must be on for it in the Govee app")
LIFX = Brand("LIFX", "lifx", "light", "its local LAN protocol on UDP port 56700, with no login")
HOMEKIT = Brand(
    "HomeKit accessory",
    "homekit",
    "accessory",
    "",
    cannot="it is paired with Apple Home, and controlling it that way needs a HomeKit pairing north cannot do yet",
)
MATTER = Brand(
    "Matter device",
    "matter",
    "device",
    "",
    cannot="Matter devices are added with a pairing code through a Matter controller, which north does not have yet",
)

_MDNS_SERVICES: dict[str, Brand] = {
    "_hue._tcp.local": HUE,
    "_shelly._tcp.local": SHELLY,
    "_elg._tcp.local": ELGATO,
    "_nanoleafapi._tcp.local": NANOLEAF,
    "_wled._tcp.local": WLED,
    "_esphomelib._tcp.local": ESPHOME,
    "_hap._tcp.local": HOMEKIT,
    "_matter._tcp.local": MATTER,
    "_matterc._udp.local": MATTER,
}
# HomeKit categories ("ci") that are lights, plugs, switches or a bridge for them; not TVs or speakers.
_HOMEKIT_KINDS = {"2": "bridge", "5": "light", "7": "plug", "8": "switch"}


@dataclass(frozen=True)
class Found:
    """One thing on the network north recognised."""

    brand: Brand
    name: str
    address: str  # "192.168.1.20" or "192.168.1.20:80"
    how: str  # "mDNS _hue._tcp"
    kind: str = ""
    details: dict[str, str] = field(default_factory=dict)

    @property
    def host(self) -> str:
        return self.address.rsplit(":", 1)[0] if self.address.count(":") == 1 else self.address

    def shown(self) -> dict[str, Any]:
        """As the Devices page shows it, and as north is told it when you choose to add it."""
        return {
            "id": f"{self.brand.key}@{self.address}",
            "brand": self.brand.name,
            "key": self.brand.key,
            "kind": self.kind or self.brand.kind,
            "name": self.name,
            "address": self.address,
            "how": self.how,
            "reach": self.brand.reach,
            "cannot": self.brand.cannot,
            "details": dict(self.details),
        }


@dataclass
class Heard:
    """What one host said about itself in any answer: its mDNS services and names, its SSDP server."""

    services: set[str] = field(default_factory=set)  # "_googlecast._tcp"
    names: set[str] = field(default_factory=set)  # "Living Room TV"
    server: str = ""  # an SSDP answer's SERVER header

    def add(self, other: Heard) -> None:
        self.services |= other.services
        self.names |= other.names
        self.server = self.server or other.server


# A probe: what to send, where, and how to read each answer. `bind` is the port answers come to,
# for a brand that answers on a port of its own (Govee) rather than to the sender. `heard` reads
# what any host said about itself, recognised or not, for the list of every device.
@dataclass(frozen=True)
class Probe:
    name: str
    send: bytes
    to: tuple[str, int]
    read: Callable[[bytes, str], list[Found]]
    bind: int = 0
    heard: Callable[[bytes, str], dict[str, Heard]] | None = None


@dataclass(frozen=True)
class Scan:
    """What one look at the network heard: the devices north recognised, and what every host said about itself."""

    found: list[Found]
    heard: dict[str, Heard]


async def discover(
    listen: Callable[[Probe, float], list[tuple[bytes, str]]] | None = None, seconds: float = LISTEN_SECONDS
) -> Scan:
    """Every device found by any probe, once each, what north can connect to first; and what every host said.

    A second, shorter mDNS round asks for the names behind each service heard, so a TV is listed as
    "Living Room TV" and not only as an address.
    """
    listen = listen or listen_udp
    answers = await asyncio.gather(*(asyncio.to_thread(listen, probe, seconds) for probe in PROBES))
    found: list[Found] = []
    heard: dict[str, Heard] = {}
    for probe, replies in zip(PROBES, answers, strict=True):
        found += _read(probe, replies, heard)
    services = sorted(
        {s for h in heard.values() for s in h.services} - {k.removesuffix(".local") for k in _MDNS_SERVICES}
    )
    if services:
        names = Probe("mDNS names", mdns_query(f"{s}.local" for s in services[:30]), _MDNS, read_mdns, heard=mdns_heard)
        found += _read(names, await asyncio.to_thread(listen, names, seconds / 2), heard)
    return Scan(_once_each(found), heard)


def _read(probe: Probe, replies: list[tuple[bytes, str]], heard: dict[str, Heard]) -> list[Found]:
    found: list[Found] = []
    for packet, sender in replies:
        heard.setdefault(sender, Heard())
        try:
            found += probe.read(packet, sender)
            for host, said in (probe.heard(packet, sender) if probe.heard else {}).items():
                heard.setdefault(host, Heard()).add(said)
        except Exception:  # noqa: BLE001 - one odd answer must not hide the others
            logger.debug("discovery: an answer to %s could not be read", probe.name, exc_info=True)
    return found


def _once_each(found: Iterable[Found]) -> list[Found]:
    """One entry per brand and host; a host north can reach hides what else it called itself.

    A Hue bridge answers mDNS, SSDP and HomeKit: shown once, as the Hue bridge north can reach.
    """
    by_host: dict[str, list[Found]] = {}
    for item in found:
        entries = by_host.setdefault(item.host, [])
        if all(existing.brand.key != item.brand.key for existing in entries):
            entries.append(item)
    kept: list[Found] = []
    for entries in by_host.values():
        reachable = [item for item in entries if not item.brand.cannot]
        kept += reachable or entries
    return sorted(kept, key=lambda item: (bool(item.brand.cannot), item.brand.name, item.name))


def listen_udp(probe: Probe, seconds: float) -> list[tuple[bytes, str]]:
    """Send *probe* twice, a second apart, and gather every answer until *seconds* have passed."""
    replies: list[tuple[bytes, str]] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.bind(("", probe.bind))
            if probe.bind and probe.to[0].startswith("239."):
                with contextlib.suppress(OSError):
                    membership = struct.pack("4s4s", socket.inet_aton(probe.to[0]), socket.inet_aton("0.0.0.0"))
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            deadline = time.monotonic() + seconds
            resend = time.monotonic() + 1.0
            sock.sendto(probe.send, probe.to)
            while (left := deadline - time.monotonic()) > 0:
                if resend and time.monotonic() >= resend:
                    with contextlib.suppress(OSError):
                        sock.sendto(probe.send, probe.to)
                    resend = 0.0
                sock.settimeout(min(left, 0.5))
                try:
                    packet, (sender, _port) = sock.recvfrom(9000)
                except TimeoutError:
                    continue
                if packet != probe.send:  # our own broadcast, heard on the way out
                    replies.append((packet, sender))
    except OSError as exc:
        logger.info("discovery: %s could not listen (%s)", probe.name, exc)
    return replies


# ── mDNS ─────────────────────────────────────────────────────────────────────

_PTR, _TXT, _SRV, _A = 12, 16, 33, 1
_MDNS = ("224.0.0.251", 5353)


def mdns_query(services: Iterable[str]) -> bytes:
    """One mDNS query asking who offers each of *services* (PTR), answered to this socket."""
    services = list(services)
    header = struct.pack("!HHHHHH", 0, 0, len(services), 0, 0, 0)
    return header + b"".join(_encode_name(service) + struct.pack("!HH", _PTR, 1) for service in services)


@dataclass
class _Mdns:
    """The records of one mDNS answer: which instances offer each service, and where each one is."""

    instances: dict[str, list[str]] = field(default_factory=dict)  # service -> instances
    srv: dict[str, tuple[int, str]] = field(default_factory=dict)  # instance -> (port, host name)
    txt: dict[str, dict[str, str]] = field(default_factory=dict)
    addresses: dict[str, str] = field(default_factory=dict)  # host name -> IPv4

    def host(self, instance: str, sender: str) -> tuple[str, int]:
        port, target = self.srv.get(instance.lower(), (0, ""))
        return self.addresses.get(target, sender), port


def _parse_mdns(packet: bytes) -> _Mdns:
    _id, _flags, questions, *counts = struct.unpack("!HHHHHH", packet[:12])
    offset = 12
    for _ in range(questions):
        _name, offset = _read_name(packet, offset)
        offset += 4
    parsed = _Mdns()
    for _ in range(sum(counts)):
        name, offset = _read_name(packet, offset)
        kind, _klass, _ttl, length = struct.unpack("!HHIH", packet[offset : offset + 10])
        offset += 10
        data = offset
        offset += length
        if kind == _PTR:
            parsed.instances.setdefault(name.lower(), []).append(_read_name(packet, data)[0])
        elif kind == _SRV:
            port = struct.unpack("!H", packet[data + 4 : data + 6])[0]
            parsed.srv[name.lower()] = (port, _read_name(packet, data + 6)[0].lower())
        elif kind == _TXT:
            parsed.txt[name.lower()] = _read_txt(packet[data:offset])
        elif kind == _A and length == 4:
            parsed.addresses[name.lower()] = socket.inet_ntoa(packet[data:offset])
    return parsed


def read_mdns(packet: bytes, sender: str) -> list[Found]:
    """The devices an mDNS answer announces, for the services north recognises."""
    parsed = _parse_mdns(packet)
    found = []
    for service, names in parsed.instances.items():
        brand = _MDNS_SERVICES.get(service)
        if brand is None:
            continue
        for instance in names:
            host, port = parsed.host(instance, sender)
            details = parsed.txt.get(instance.lower(), {})
            kind = ""
            if brand is HOMEKIT:
                kind = _HOMEKIT_KINDS.get(details.get("ci", ""), "")
                if not kind:
                    continue  # a TV or a speaker: not for the Devices page
            label = _label(instance, service)
            address = f"{host}:{port}" if port and port not in (80, 443) else host
            found.append(Found(brand, label, address, f"mDNS {service.removesuffix('.local')}", kind, details))
    return found


def mdns_heard(packet: bytes, sender: str) -> dict[str, Heard]:
    """What each host in an mDNS answer offers ("_googlecast._tcp") and calls itself ("Living Room TV")."""
    parsed = _parse_mdns(packet)
    heard: dict[str, Heard] = {}
    for service, names in parsed.instances.items():
        for instance in names:
            if service == "_services._dns-sd._udp.local":
                heard.setdefault(sender, Heard()).services.add(instance.lower().removesuffix(".local"))
                continue
            host, _port = parsed.host(instance, sender)
            said = heard.setdefault(host, Heard())
            said.services.add(service.removesuffix(".local"))
            if label := _label(instance, service):
                said.names.add(label)
    return heard


def _label(instance: str, service: str) -> str:
    """An instance's own name: "Living Room TV" from "Living Room TV._googlecast._tcp.local"."""
    return instance[: -len(service) - 1] if instance.lower().endswith("." + service) else instance


def _encode_name(name: str) -> bytes:
    return b"".join(bytes([len(part)]) + part.encode() for part in name.rstrip(".").split(".")) + b"\0"


def _read_name(packet: bytes, offset: int) -> tuple[str, int]:
    """A DNS name at *offset*, following compression pointers; and where the record goes on."""
    labels: list[str] = []
    end = -1
    for _ in range(64):  # a pointer loop in a hostile packet ends here
        length = packet[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:
            if end < 0:
                end = offset + 2
            offset = struct.unpack("!H", packet[offset : offset + 2])[0] & 0x3FFF
            continue
        labels.append(packet[offset + 1 : offset + 1 + length].decode("utf-8", "replace"))
        offset += 1 + length
    return ".".join(labels), (end if end >= 0 else offset)


def _read_txt(data: bytes) -> dict[str, str]:
    pairs: dict[str, str] = {}
    index = 0
    while index < len(data):
        length = data[index]
        entry = data[index + 1 : index + 1 + length].decode("utf-8", "replace")
        index += 1 + length
        key, _, value = entry.partition("=")
        if key:
            pairs[key.lower()] = value
    return pairs


# ── SSDP, and Yeelight's own version of it ───────────────────────────────────


def _headers(packet: bytes) -> dict[str, str]:
    lines = packet.decode("utf-8", "replace").split("\r\n")
    return {key.strip().lower(): value.strip() for key, _, value in (line.partition(":") for line in lines[1:]) if key}


_WEMO_KINDS = {"socket": "plug", "insight": "plug", "lightswitch": "switch", "dimmer": "switch", "bridge": "bridge"}


def read_ssdp(packet: bytes, sender: str) -> list[Found]:
    """A Hue bridge or a WeMo device answering an SSDP search; anything else (a TV, a router) is left out."""
    headers = _headers(packet)
    if "hue-bridgeid" in headers or "ipbridge" in headers.get("server", "").lower():
        return [Found(HUE, "Hue bridge", sender, "SSDP", details={"bridgeid": headers.get("hue-bridgeid", "")})]
    usn = headers.get("usn", "").lower()
    if "belkin" in headers.get("st", "").lower() or any(f"uuid:{k}-" in usn for k in _WEMO_KINDS):
        kind = next((v for k, v in _WEMO_KINDS.items() if f"uuid:{k}-" in usn), "plug")
        location = headers.get("location", "")
        address = location.split("//", 1)[-1].split("/", 1)[0] if location else sender
        return [Found(WEMO, f"WeMo {kind}", address, "SSDP", kind, {"usn": headers.get("usn", "")})]
    return []


def ssdp_heard(packet: bytes, sender: str) -> dict[str, Heard]:
    server = _headers(packet).get("server", "")
    return {sender: Heard(server=server)} if server else {}


def read_yeelight(packet: bytes, sender: str) -> list[Found]:
    headers = _headers(packet)
    address = headers.get("location", "").removeprefix("yeelight://") or sender
    details: dict[str, str] = {key: headers[key] for key in ("id", "model", "support") if key in headers}
    return [
        Found(
            YEELIGHT,
            headers.get("name") or f"Yeelight {headers.get('model', 'light')}",
            address,
            "Yeelight search",
            details=details,
        )
    ]


# ── WiZ, Govee and LIFX, each in its own words ────────────────────────────────


def read_wiz(packet: bytes, sender: str) -> list[Found]:
    result = json.loads(packet).get("result") or {}
    module = str(result.get("moduleName") or "")
    kind = "plug" if "SOCKET" in module.upper() else "light"
    details = {key: str(result[key]) for key in ("mac", "moduleName", "fwVersion") if key in result}
    return [Found(WIZ, f"WiZ {kind}", sender, "WiZ discovery", kind, details)]


def read_govee(packet: bytes, sender: str) -> list[Found]:
    data = (json.loads(packet).get("msg") or {}).get("data") or {}
    if not data.get("ip"):
        return []
    details = {key: str(data[key]) for key in ("device", "sku") if key in data}
    return [Found(GOVEE, f"Govee {data.get('sku', 'light')}", str(data["ip"]), "Govee LAN scan", details=details)]


_LIFX_GET_SERVICE = (
    struct.pack("<HHI", 36, 0x3400, 0x6E6F7274) + bytes(8) + bytes(6) + bytes(2) + bytes(8) + struct.pack("<HH", 2, 0)
)


def read_lifx(packet: bytes, sender: str) -> list[Found]:
    if len(packet) < 41 or struct.unpack("<H", packet[32:34])[0] != 3:  # StateService
        return []
    mac = ":".join(f"{byte:02x}" for byte in packet[8:14])
    return [Found(LIFX, "LIFX light", sender, "LIFX discovery", details={"mac": mac})]


PROBES: tuple[Probe, ...] = (
    # Also asks which services exist at all, so every mDNS device answers and says what it offers.
    Probe("mDNS", mdns_query([*_MDNS_SERVICES, "_services._dns-sd._udp.local"]), _MDNS, read_mdns, heard=mdns_heard),
    Probe(
        "SSDP",
        b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 2\r\nST: ssdp:all\r\n\r\n',
        ("239.255.255.250", 1900),
        read_ssdp,
        heard=ssdp_heard,
    ),
    Probe(
        "Yeelight",
        b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1982\r\nMAN: "ssdp:discover"\r\nST: wifi_bulb\r\n',
        ("239.255.255.250", 1982),
        read_yeelight,
    ),
    Probe("WiZ", b'{"method":"getSystemConfig","params":{}}', ("255.255.255.255", 38899), read_wiz),
    Probe(
        "Govee",
        b'{"msg":{"cmd":"scan","data":{"account_topic":"reserve"}}}',
        ("239.255.255.250", 4001),
        read_govee,
        bind=4002,
    ),
    Probe("LIFX", _LIFX_GET_SERVICE, ("255.255.255.255", 56700), read_lifx),
)
