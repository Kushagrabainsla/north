"""Find devices: what is on your network that north could control.

Adding a device was a box to type in. Now the Devices page looks on the network the
ways lights, plugs and switches announce themselves, lists what it recognises, and says
for each how north would reach it or why it cannot yet. Each answer here is shaped as
the device sends it.
"""

from __future__ import annotations

import socket
import struct
import threading

from tools import home_discovery as discovery
from tools.home_discovery import GOVEE, HOMEKIT, HUE, LIFX, SHELLY, WEMO, WIZ, YEELIGHT, Probe


class _Answer:
    """An mDNS answer as a responder writes it: names compressed against the ones before them."""

    def __init__(self) -> None:
        self.data = bytearray(12)
        self.offsets: dict[str, int] = {}
        self.count = 0

    def name(self, name: str) -> bytes:
        labels = name.split(".")
        out = bytearray()
        for index in range(len(labels)):
            suffix = ".".join(labels[index:])
            if suffix in self.offsets:
                return bytes(out + struct.pack("!H", 0xC000 | self.offsets[suffix]))
            self.offsets[suffix] = len(self.data) + len(out)
            out += bytes([len(labels[index].encode())]) + labels[index].encode()
        return bytes(out + b"\0")

    def record(self, owner: str, kind: int, write) -> None:
        self.data += self.name(owner)
        self.data += struct.pack("!HHIH", kind, 0x8001, 120, 0)
        start = len(self.data)
        write(self)
        struct.pack_into("!H", self.data, start - 2, len(self.data) - start)
        self.count += 1

    def ptr(self, service: str, instance: str) -> None:
        self.record(service, 12, lambda a: a.data.extend(a.name(instance)))

    def srv(self, instance: str, port: int, target: str) -> None:
        def write(a: _Answer) -> None:
            a.data.extend(struct.pack("!HHH", 0, 0, port))
            a.data.extend(a.name(target))  # after the numbers, so a later pointer to this name lands on it

        self.record(instance, 33, write)

    def txt(self, instance: str, **pairs: str) -> None:
        entries = b"".join(bytes([len(e)]) + e for e in (f"{k}={v}".encode() for k, v in pairs.items()))
        self.record(instance, 16, lambda a: a.data.extend(entries))

    def a(self, host: str, address: str) -> None:
        self.record(host, 1, lambda a: a.data.extend(socket.inet_aton(address)))

    def packet(self) -> bytes:
        struct.pack_into("!HHHHHH", self.data, 0, 0, 0x8400, 0, self.count, 0, 0)
        return bytes(self.data)


def _hue_mdns() -> bytes:
    answer = _Answer()
    instance = "Hue Bridge - 1A2B3C._hue._tcp.local"
    answer.ptr("_hue._tcp.local", instance)
    answer.srv(instance, 443, "001788fffe1a2b3c.local")
    answer.txt(instance, bridgeid="001788fffe1a2b3c", modelid="BSB002")
    answer.a("001788fffe1a2b3c.local", "192.168.1.20")
    return answer.packet()


_HUE_SSDP = (
    b"HTTP/1.1 200 OK\r\nHOST: 239.255.255.250:1900\r\nEXT:\r\nCACHE-CONTROL: max-age=100\r\n"
    b"LOCATION: http://192.168.1.20:80/description.xml\r\nSERVER: Linux/3.14.0 UPnP/1.0 IpBridge/1.48.0\r\n"
    b"hue-bridgeid: 001788FFFE1A2B3C\r\nST: upnp:rootdevice\r\n"
    b"USN: uuid:2f402f80-da50-11e1-9b23-001788102201::upnp:rootdevice\r\n\r\n"
)
_WEMO_SSDP = (
    b"HTTP/1.1 200 OK\r\nLOCATION: http://192.168.1.40:49153/setup.xml\r\nST: urn:Belkin:device:controllee:1\r\n"
    b"USN: uuid:Socket-1_0-221517K0101769::urn:Belkin:device:controllee:1\r\n\r\n"
)
_ROUTER_SSDP = (
    b"HTTP/1.1 200 OK\r\nSERVER: Linux UPnP/1.0 miniupnpd/2.0\r\n"
    b"ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n"
)
_WIZ = (
    b'{"method":"getSystemConfig","env":"pro","result":{"mac":"a8bb50aabbcc","homeId":123,"roomId":456,'
    b'"moduleName":"ESP01_SHRGB1C_31","fwVersion":"1.21.0","groupId":0}}'
)
_GOVEE = (
    b'{"msg":{"cmd":"scan","data":{"ip":"192.168.1.23","device":"1F:80:C5:32:32:36:72:4E","sku":"H618E",'
    b'"bleVersionHard":"3.01.01","wifiVersionSoft":"1.02.03"}}}'
)
_YEELIGHT = (
    b"HTTP/1.1 200 OK\r\nCache-Control: max-age=3600\r\nLocation: yeelight://192.168.1.239:55443\r\n"
    b"Server: POSIX UPnP/1.0 YGLC/1\r\nid: 0x000000000015243f\r\nmodel: color\r\nfw_ver: 18\r\n"
    b"support: get_prop set_power toggle set_bright\r\npower: on\r\nbright: 100\r\nname: Desk bulb\r\n"
)
_LIFX = (
    struct.pack("<HHI", 41, 0x1400, 1234)
    + bytes.fromhex("d073d5123456")
    + bytes(2)
    + bytes(6)
    + bytes(2)
    + bytes(8)
    + struct.pack("<HH", 3, 0)
    + bytes([1])
    + struct.pack("<I", 56700)
)


def test_a_hue_bridge_announced_by_mdns_is_found_with_how_north_reaches_it() -> None:
    [bridge] = discovery.read_mdns(_hue_mdns(), "192.168.1.20")

    shown = bridge.shown()
    assert (bridge.brand, bridge.name, bridge.address) == (HUE, "Hue Bridge - 1A2B3C", "192.168.1.20")
    assert shown["how"] == "mDNS _hue._tcp" and shown["kind"] == "bridge" and shown["cannot"] == ""
    assert shown["details"] == {"bridgeid": "001788fffe1a2b3c", "modelid": "BSB002"}
    assert "link button" in shown["reach"]


def test_a_device_on_its_own_port_keeps_it_and_one_without_an_address_record_uses_the_sender() -> None:
    answer = _Answer()
    answer.ptr("_elg._tcp.local", "Elgato Key Light 1A2B._elg._tcp.local")
    answer.srv("Elgato Key Light 1A2B._elg._tcp.local", 9123, "elgato-key-light-1a2b.local")
    answer.ptr("_shelly._tcp.local", "shellyplus1-abc._shelly._tcp.local")
    answer.srv("shellyplus1-abc._shelly._tcp.local", 80, "shellyplus1-abc.local")
    answer.a("shellyplus1-abc.local", "192.168.1.30")

    found = {f.brand.key: f for f in discovery.read_mdns(answer.packet(), "192.168.1.31")}

    assert found["elgato"].address == "192.168.1.31:9123"  # no A record: the answer came from there
    assert found["shelly"].address == "192.168.1.30" and found["shelly"].brand is SHELLY


def test_homekit_lights_plugs_and_switches_are_listed_as_not_yet_and_a_tv_is_left_out() -> None:
    answer = _Answer()
    answer.ptr("_hap._tcp.local", "Hall Lamp._hap._tcp.local")
    answer.txt("Hall Lamp._hap._tcp.local", ci="5", md="Eve Light Strip")
    answer.ptr("_hap._tcp.local", "Living Room TV._hap._tcp.local")
    answer.txt("Living Room TV._hap._tcp.local", ci="31", md="TV")

    [lamp] = discovery.read_mdns(answer.packet(), "192.168.1.50")

    assert lamp.brand is HOMEKIT and lamp.kind == "light" and lamp.name == "Hall Lamp"
    assert "HomeKit pairing north cannot do yet" in lamp.shown()["cannot"]


def test_ssdp_finds_a_hue_bridge_and_a_wemo_plug_and_leaves_a_router_out() -> None:
    [hue] = discovery.read_ssdp(_HUE_SSDP, "192.168.1.20")
    [wemo] = discovery.read_ssdp(_WEMO_SSDP, "192.168.1.40")

    assert hue.brand is HUE and hue.details == {"bridgeid": "001788FFFE1A2B3C"}
    assert (wemo.brand, wemo.kind, wemo.address) == (WEMO, "plug", "192.168.1.40:49153")
    assert discovery.read_ssdp(_ROUTER_SSDP, "192.168.1.1") == []


def test_wiz_govee_yeelight_and_lifx_are_read_from_their_own_answers() -> None:
    [wiz] = discovery.read_wiz(_WIZ, "192.168.1.60")
    [govee] = discovery.read_govee(_GOVEE, "192.168.1.23")
    [yeelight] = discovery.read_yeelight(_YEELIGHT, "192.168.1.239")
    [lifx] = discovery.read_lifx(_LIFX, "192.168.1.70")

    assert (wiz.brand, wiz.kind, wiz.details["moduleName"]) == (WIZ, "light", "ESP01_SHRGB1C_31")
    assert (govee.brand, govee.address, govee.details["sku"]) == (GOVEE, "192.168.1.23", "H618E")
    assert (yeelight.brand, yeelight.name, yeelight.address) == (YEELIGHT, "Desk bulb", "192.168.1.239:55443")
    assert (lifx.brand, lifx.details["mac"]) == (LIFX, "d0:73:d5:12:34:56")
    socket_plug = _WIZ.replace(b"ESP01_SHRGB1C_31", b"ESP10_SOCKET_06")
    assert discovery.read_wiz(socket_plug, "192.168.1.61")[0].kind == "plug"


async def test_one_bridge_announcing_itself_three_ways_is_listed_once_and_what_north_can_reach_comes_first() -> None:
    homekit = _Answer()
    homekit.ptr("_hap._tcp.local", "Hue Bridge._hap._tcp.local")
    homekit.txt("Hue Bridge._hap._tcp.local", ci="2")
    answers = {
        "mDNS": [(_hue_mdns(), "192.168.1.20"), (homekit.packet(), "192.168.1.20")],
        "SSDP": [(_HUE_SSDP, "192.168.1.20"), (_ROUTER_SSDP, "192.168.1.1")],
        "WiZ": [(_WIZ, "192.168.1.60")],
    }
    lamp = _Answer()
    lamp.ptr("_hap._tcp.local", "Eve Lamp._hap._tcp.local")
    lamp.txt("Eve Lamp._hap._tcp.local", ci="5")
    answers["mDNS"].append((lamp.packet(), "192.168.1.50"))

    tv = _Answer()
    tv.ptr("_services._dns-sd._udp.local", "_googlecast._tcp.local")  # a TV saying what it offers
    answers["mDNS"].append((tv.packet(), "192.168.1.80"))

    scan = await discovery.discover(lambda probe, _seconds: answers.get(probe.name, []))

    assert [(f.brand.key, f.host) for f in scan.found] == [
        ("hue", "192.168.1.20"),
        ("wiz", "192.168.1.60"),
        ("homekit", "192.168.1.50"),  # north cannot reach it: last
    ]
    assert scan.heard["192.168.1.80"].services == {"_googlecast._tcp"}  # a TV: heard, not a light
    assert scan.heard["192.168.1.1"].server == "Linux UPnP/1.0 miniupnpd/2.0"  # the router


async def test_an_answer_that_cannot_be_read_hides_nothing_else() -> None:
    loop = bytearray(struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0)) + b"\xc0\x0c"  # a name that points at itself
    answers = {"mDNS": [(bytes(loop), "192.168.1.9")], "WiZ": [(b"not json", "192.168.1.8"), (_WIZ, "192.168.1.60")]}

    scan = await discovery.discover(lambda probe, _seconds: answers.get(probe.name, []))

    assert [f.brand for f in scan.found] == [WIZ]


def test_a_probe_is_sent_and_its_answers_are_heard() -> None:
    responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    responder.bind(("127.0.0.1", 0))
    responder.settimeout(3)

    def answer() -> None:
        with responder:
            _packet, sender = responder.recvfrom(2048)
            responder.sendto(_WIZ, sender)

    threading.Thread(target=answer, daemon=True).start()
    probe = Probe("WiZ", b'{"method":"getSystemConfig","params":{}}', responder.getsockname(), discovery.read_wiz)

    replies = discovery.listen_udp(probe, 1.2)

    assert replies == [(_WIZ, "127.0.0.1")]


async def test_names_behind_each_service_are_asked_for_so_a_tv_is_named() -> None:
    """The first round hears that a host offers Google Cast; the second asks who, and it answers "Living Room TV"."""
    services = _Answer()
    services.ptr("_services._dns-sd._udp.local", "_googlecast._tcp.local")
    named = _Answer()
    named.ptr("_googlecast._tcp.local", "Living Room TV._googlecast._tcp.local")
    named.srv("Living Room TV._googlecast._tcp.local", 8009, "tv.local")
    named.a("tv.local", "192.168.1.80")
    asked: list[bytes] = []

    def listen(probe: Probe, _seconds: float) -> list[tuple[bytes, str]]:
        asked.append(probe.send)
        if probe.name == "mDNS":
            return [(services.packet(), "192.168.1.80")]
        return [(named.packet(), "192.168.1.80")] if probe.name == "mDNS names" else []

    scan = await discovery.discover(listen)

    assert b"_googlecast" in asked[-1]  # the second round asked for that service's names
    assert scan.heard["192.168.1.80"].names == {"Living Room TV"} and scan.found == []
