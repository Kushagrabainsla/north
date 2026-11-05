"""Every device on your home network, named as far as north can tell.

Find devices listed only what announced itself as a brand north knows. The user asked for
every device, and the choice to connect to any of them. Now north wakes each address around
this computer, reads which answered from the network's address table, and names each one by
its network card's maker, the name the router gives it, its web page, its services and its
ports; any device can be tried.
"""

from __future__ import annotations

import httpx
import pytest

from tools import home_network as network
from tools.home import Home
from tools.home_discovery import HOMEKIT, HUE, Found, Heard, Scan
from tools.home_network import PRIVATE, NetworkDevice, survey
from tools.registry import ToolRegistry

_PROC_ARP = """IP address       HW type     Flags       HW address            Mask     Device
10.0.0.1         0x1         0x2         a0:b1:c2:d3:e4:f5     *        eth0
10.0.0.47        0x1         0x2         50:c7:bf:00:00:01     *        eth0
10.0.0.99        0x1         0x0         00:00:00:00:00:00     *        eth0
"""
_MAC_ARP = """? (10.0.0.1) at a0:b1:c2:d3:e4:f5 on en0 ifscope [ethernet]
? (10.0.0.55) at a4:cf:12:a:b:c on en0 ifscope [ethernet]
? (10.0.0.60) at (incomplete) on en0 ifscope [ethernet]
? (224.0.0.251) at 1:0:5e:0:0:fb on en0 ifscope permanent [ethernet]
"""
_PROC_ROUTE = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
eth0\t0000000A\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0
eth0\t00000000\t0100000A\t0003\t0\t0\t0\t00000000\t0\t0\t0
"""


def test_the_address_tables_are_read_as_linux_and_macos_print_them() -> None:
    assert network.parse_proc_arp(_PROC_ARP) == {"10.0.0.1": "a0:b1:c2:d3:e4:f5", "10.0.0.47": "50:c7:bf:00:00:01"}
    assert network.parse_arp(_MAC_ARP) == {
        "10.0.0.1": "a0:b1:c2:d3:e4:f5",
        "10.0.0.55": "a4:cf:12:0a:0b:0c",  # macOS drops leading zeros
        "224.0.0.251": "01:00:5e:00:00:fb",
    }
    assert network.parse_proc_route(_PROC_ROUTE) == "10.0.0.1"


def test_a_network_card_names_its_maker_and_a_made_up_address_says_what_it_likely_is() -> None:
    assert network.maker("00:17:88:aa:bb:cc") == "Philips Hue (Signify)"
    assert network.maker("a4:cf:12:aa:bb:cc") == "Espressif"
    assert network.maker("50:c7:bf:00:00:01") == "TP-Link"
    assert network.maker("a6:11:22:33:44:55") == PRIVATE
    assert NetworkDevice("10.0.0.70", maker=PRIVATE).name() == "a phone, tablet or computer"


class _Home:
    """A home network: which addresses answer on which ports, their web pages and their names."""

    def __init__(self) -> None:
        self.ports = {"10.0.0.55": {80, 6668}, "10.0.0.20": {80}, "10.0.0.31": {22}}
        self.pages = {"10.0.0.55": "Tasmota", "10.0.0.20": "Hue - Philips"}
        self.names = {"10.0.0.31": "Kushs-MacBook-Pro.lan", "10.0.0.55": "ESP_AABBCC.lan"}

    async def answers(self, host: str, port: int) -> bool:
        return port in self.ports.get(host, set())

    async def page(self, host: str, port: int) -> str:
        return self.pages.get(host, "")

    def hostname(self, host: str) -> str:
        return self.names.get(host, "")


async def test_every_device_is_listed_once_with_what_north_could_tell_and_what_it_can_do_with_it() -> None:
    table = (
        "10.0.0.12",
        "10.0.0.1",
        {
            "10.0.0.1": "a0:b1:c2:d3:e4:f5",
            "10.0.0.20": "00:17:88:aa:bb:cc",
            "10.0.0.55": "a4:cf:12:aa:bb:cc",
            "10.0.0.31": "f0:18:98:00:00:01",
            "10.0.0.80": "a6:11:22:33:44:55",
            "10.0.0.223": "00:d0:2d:00:00:01",
        },
    )
    scan = Scan(
        found=[
            Found(HUE, "Hue Bridge - 1A2B3C", "10.0.0.20", "mDNS _hue._tcp"),
            Found(HOMEKIT, "TSTAT-276BCD", "10.0.0.223", "mDNS _hap._tcp", "bridge"),
        ],
        heard={"10.0.0.80": Heard(services={"_googlecast._tcp"}, names={"Living Room TV"})},
    )

    devices = {d.address: d for d in await survey(scan, table, _Home())}

    assert devices["10.0.0.20"].connect == "add" and devices["10.0.0.20"].maker == "Philips Hue (Signify)"
    esp = devices["10.0.0.55"].shown()
    assert esp["connect"] == "try" and esp["name"] == "ESP_AABBCC" and esp["maker"] == "Espressif"
    assert 'a web page, "Tasmota"' in esp["offers"] and any("Tuya" in offer for offer in esp["offers"])
    assert devices["10.0.0.80"].name() == "Living Room TV" and "Google Cast" in devices["10.0.0.80"].offers
    assert devices["10.0.0.223"].connect == "try"  # HomeKit: north cannot pair, but the user may still try
    assert devices["10.0.0.12"].role == "this computer" and devices["10.0.0.1"].role == "your router"
    assert devices["10.0.0.12"].name() == "This computer, where North runs"
    assert devices["10.0.0.1"].name() == "Your router"
    order = [d.address for d in await survey(scan, table, _Home())]
    assert order[0] == "10.0.0.20" and order[-2:] == ["10.0.0.1", "10.0.0.12"]
    assert order.index("10.0.0.55") < order.index("10.0.0.31")  # a smart-home chip before a laptop


async def test_a_brand_north_has_a_tool_for_and_a_kasa_lamp_are_marked_as_already_here() -> None:
    from tests.unit.tools.test_home import _Hue

    registry = ToolRegistry(auto_register=False)
    registry.register(_Hue())

    async def on_the_network() -> list[NetworkDevice]:
        return [NetworkDevice("10.0.0.20", found=Found(HUE, "Hue bridge", "10.0.0.20", "SSDP"))]

    [bridge] = (await Home(registry, find_on_network=on_the_network).discover())["devices"]

    assert bridge["here"] == "hue_lights" and bridge["connect"] == "here"


async def test_a_web_page_at_home_is_never_fetched_through_a_proxy(monkeypatch) -> None:
    """An HTTPS proxy set for the internet would carry, or refuse, a request to a plug in the hall."""
    made: dict = {}
    client = httpx.AsyncClient

    def tracked(**kwargs):
        made.update(kwargs)
        page = httpx.Response(200, text="<html><head><title> Shelly 1PM\n</title></head></html>")
        return client(transport=httpx.MockTransport(lambda request: page), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", tracked)

    title = await network.LocalNetwork().page("10.0.0.55", 80)

    assert title == "Shelly 1PM" and made["trust_env"] is False


@pytest.mark.parametrize(("name", "plain"), [("Living-Room-TV.lan", "Living-Room-TV"), ("localhost", ""), ("", "")])
def test_a_routers_name_for_a_device_drops_the_home_domain(name: str, plain: str) -> None:
    assert network._plain_hostname(name) == plain
