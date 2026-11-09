"""Your Kasa devices, found once and kept connected: the one place north reaches them.

Every command used to search the network, connect, act and disconnect, 2-5 s each:
too slow for a slider on the dashboard and far too slow to follow music.
"""

from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import pytest
from kasa.exceptions import DeviceError, KasaException

from config import secret_store
from tests.unit.tools.kasa_fakes import FakeBulb, FakeNetwork
from tools.specialized import kasa_devices
from tools.specialized.kasa_devices import KasaDevices, LightChange

_JSON_TWO_BULBS = """
{
  "10.0.0.36": {"ip": "10.0.0.36", "device_model": "KL125(US)", "alias": "Desk lamp"},
  "10.0.0.47": {"ip": "10.0.0.47", "device_model": "KL125(US)"}
}
"""


def test_discovery_detaches_stdin_and_parses_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parsed from --json, because the CLI's prose changed underneath the scraper.

    It looked for "Host: 10.0.0.36" and python-kasa 0.10 prints "IP: 10.0.0.36",
    so two bulbs on the network read as none at all.
    """
    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        calls.append({**kwargs, "cmd": cmd})
        return SimpleNamespace(returncode=0, stdout=_JSON_TWO_BULBS, stderr="")

    monkeypatch.setattr(kasa_devices.shutil, "which", lambda _: "/usr/bin/kasa")
    monkeypatch.setattr(kasa_devices.subprocess, "run", fake_run)

    pairs, diagnostic = kasa_devices.run_discovery()

    # A device that has not authenticated reports no alias; the model stands in.
    assert pairs == [("10.0.0.36", "Desk lamp"), ("10.0.0.47", "KL125(US)")]
    assert diagnostic == ""
    assert calls[0]["stdin"] is kasa_devices.subprocess.DEVNULL
    assert "--json" in calls[0]["cmd"]


def test_discovery_uses_module_fallback_after_binary_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        if len(commands) == 1:
            return SimpleNamespace(returncode=1, stdout="", stderr="binary failed")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kasa_devices.shutil, "which", lambda _: "/usr/bin/kasa")
    monkeypatch.setattr(kasa_devices.subprocess, "run", fake_run)

    pairs, diagnostic = kasa_devices.run_discovery()

    assert pairs == []
    # Never silent. Finding nothing and saying nothing is the failure this tool
    # spent months producing: two bulbs present, "no devices found" reported.
    assert diagnostic
    assert commands == [
        ["/usr/bin/kasa", "--json", "discover"],
        [kasa_devices.sys.executable, "-m", "kasa", "--json", "discover"],
    ]


def test_devices_that_refuse_the_login_say_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real fault on a modern network, and the one that reached the user as silence."""
    monkeypatch.setattr(kasa_devices.shutil, "which", lambda _: "/usr/bin/kasa")
    monkeypatch.setattr(
        kasa_devices.subprocess,
        "run",
        lambda cmd, **kw: SimpleNamespace(
            returncode=0, stdout="", stderr="== Authentication failed for device ==\n== Authentication failed ==\n"
        ),
    )

    pairs, diagnostic = kasa_devices.run_discovery()

    assert pairs == []
    assert "2 device(s) refused authentication" in diagnostic
    assert "Settings, Secrets, as kasa_email and kasa_password" in diagnostic


def test_the_login_is_passed_to_discovery() -> None:
    """The TP-Link login is one of your secrets: kasa_email and kasa_password."""
    secret_store.store().save("kasa_email", "me@example.com")
    secret_store.store().save("kasa_password", "hunter2")

    assert kasa_devices.credential_env() == {"KASA_USERNAME": "me@example.com", "KASA_PASSWORD": "hunter2"}


def test_no_login_configured_passes_none() -> None:
    assert kasa_devices.credential_env() == {}


def test_the_password_goes_by_the_environment_not_the_command_line(monkeypatch) -> None:
    """Any program on the Mac can read a command's arguments with `ps`."""
    secret_store.store().save("kasa_email", "me@example.com")
    secret_store.store().save("kasa_password", "hunter2")
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs["env"]))
        return subprocess.CompletedProcess(cmd, 1, "", "boom")

    monkeypatch.setattr(kasa_devices.subprocess, "run", fake_run)

    _, diagnostic = kasa_devices.run_discovery()

    assert calls and all("hunter2" not in " ".join(cmd) for cmd, _ in calls)
    assert all(env["KASA_PASSWORD"] == "hunter2" for _, env in calls)
    assert "hunter2" not in diagnostic


def test_unparseable_output_is_not_a_device() -> None:
    assert kasa_devices.parse_discovery("not json at all") == []
    assert kasa_devices.parse_discovery("[]") == []


def _devices(network: FakeNetwork) -> KasaDevices:
    return KasaDevices(discover=network.discover, open_device=network.open)


async def test_devices_are_found_once_and_stay_connected() -> None:
    network = FakeNetwork(FakeBulb("10.0.0.36", "Desk lamp"), FakeBulb("10.0.0.47", "Floor lamp"))
    devices = _devices(network)

    first = await devices.devices()
    await devices.apply(first[0]["id"], LightChange(on=True))
    await devices.devices()

    assert network.discoveries == 1 and network.opened == ["10.0.0.36", "10.0.0.47"]
    assert [d["name"] for d in first] == ["Desk lamp", "Floor lamp"]


async def test_looking_again_finds_new_devices_and_keeps_the_known_ones() -> None:
    network = FakeNetwork(FakeBulb("10.0.0.36", "Desk lamp"))
    devices = _devices(network)
    await devices.devices()
    network.bulbs["10.0.0.50"] = FakeBulb("10.0.0.50", "Porch light")

    found = await devices.devices(look_again=True)

    assert network.discoveries == 2 and network.opened == ["10.0.0.36", "10.0.0.50"]
    assert {d["name"] for d in found} == {"Desk lamp", "Porch light"}


async def test_a_change_is_one_request_with_only_what_the_bulb_can_do() -> None:
    lamp = FakeBulb("10.0.0.36", "Desk lamp", light={"white": (2700, 6500)})
    devices = _devices(FakeNetwork(lamp))
    [entry] = await devices.devices()

    colour = await devices.apply(entry["id"], LightChange(hue=240, saturation=80, brightness=60))
    white = await devices.apply(entry["id"], LightChange(kelvin=2500))  # below this bulb's range

    first, second = lamp.light.sent
    assert (first.hue, first.saturation, first.brightness, first.color_temp) == (240, 80, 60, 0)  # 0: colour mode
    assert second.color_temp == 2700
    assert colour["hue"] == 240 and colour["on"] and white["kelvin"] == 2700
    assert entry["can"] == {"dim": True, "color": True, "white": [2700, 6500]}


async def test_what_a_device_cannot_do_is_refused_before_anything_is_sent() -> None:
    white_only = FakeBulb("10.0.0.36", "Hall light", light={"color": False})
    plug = FakeBulb("10.0.0.40", "Fan plug", plug=True)
    devices = _devices(FakeNetwork(white_only, plug))
    entries = {d["name"]: d for d in await devices.devices()}

    with pytest.raises(ValueError, match="does not support"):
        await devices.apply(entries["Hall light"]["id"], LightChange(hue=120, saturation=100))
    with pytest.raises(ValueError, match="only be switched on and off"):
        await devices.apply(entries["Fan plug"]["id"], LightChange(brightness=40))
    on = await devices.apply(entries["Fan plug"]["id"], LightChange(on=True))

    assert white_only.light.sent == [] and on["on"] and entries["Fan plug"]["kind"] == "switch"


async def test_a_dropped_connection_is_opened_again_once() -> None:
    lamp = FakeBulb("10.0.0.36", "Desk lamp")
    network = FakeNetwork(lamp)
    devices = _devices(network)
    [entry] = await devices.devices()
    lamp.down = True

    state = await devices.apply(entry["id"], LightChange(brightness=70))

    assert state["brightness"] == 70 and network.opened == ["10.0.0.36", "10.0.0.36"]


async def test_a_bulb_given_a_new_address_is_reached_there() -> None:
    """A router gave the lamp a new address: north asked only the old one, and every change failed
    until its next look round the network, up to five minutes later."""
    lamp = FakeBulb("10.0.0.36", "Desk lamp")
    network = FakeNetwork(lamp)
    devices = _devices(network)
    [entry] = await devices.devices()
    lamp.down = True
    moved = FakeBulb("10.0.0.52", "Desk lamp")
    network.bulbs = {"10.0.0.52": moved}

    state = await devices.apply(entry["id"], LightChange(on=True))

    assert state["host"] == "10.0.0.52" and moved.is_on and lamp.closed
    assert network.opened == ["10.0.0.36", "10.0.0.52"]


@pytest.mark.parametrize(
    ("others", "why"),
    [
        (True, r"not heard on the network \(it was at 10\.0\.0\.36\)\. Is it switched on at the wall\?"),
        (False, r"No Kasa device answers on this network\. Is this computer on the same Wi-Fi as your lights\?"),
    ],
)
async def test_a_bulb_heard_nowhere_says_what_to_check(others: bool, why: str) -> None:
    """On another Wi-Fi, the page said "78:20:51:59:45:36: Timed out getting discovery response for 10.0.0.47"."""
    lamp, porch = FakeBulb("10.0.0.36", "Desk lamp"), FakeBulb("10.0.0.40", "Porch light")
    network = FakeNetwork(lamp, porch)
    devices = _devices(network)
    entries = {d["name"]: d for d in await devices.devices()}
    lamp.down = True
    network.bulbs = {"10.0.0.40": porch} if others else {}

    with pytest.raises(kasa_devices.NotAnswering, match=f"^{why}$"):
        await devices.apply(entries["Desk lamp"]["id"], LightChange(on=True))


def _wrapped(cause: Exception):
    """A request as python-kasa fails it for an older bulb: its own error, with what happened beneath."""

    async def send(state):
        try:
            raise cause
        except Exception as error:
            raise KasaException("Communication error on smartlife.iot.smartbulb.lightingservice") from error

    return send


async def test_a_refused_connection_python_kasa_reports_as_its_own_error_is_looked_for_too() -> None:
    """Live, with the bulb gone, the page said "Communication error on ...", and north never looked for it."""
    lamp = FakeBulb("10.0.0.36", "Desk lamp")
    network = FakeNetwork(lamp)
    devices = _devices(network)
    [entry] = await devices.devices()
    lamp.light.set_state = _wrapped(ConnectionRefusedError(61, "Connection refused"))
    network.bulbs = {}

    with pytest.raises(kasa_devices.NotAnswering, match="^No Kasa device answers on this network"):
        await devices.apply(entry["id"], LightChange(on=True))
    assert network.discoveries == 2


async def test_a_bulb_that_answers_with_an_error_is_not_looked_for() -> None:
    lamp = FakeBulb("10.0.0.36", "Desk lamp")
    network = FakeNetwork(lamp)
    devices = _devices(network)
    [entry] = await devices.devices()
    lamp.light.set_state = _wrapped(DeviceError("the bulb says no"))

    with pytest.raises(KasaException, match="Communication error"):
        await devices.apply(entry["id"], LightChange(on=True))
    assert network.discoveries == 1


async def test_a_look_that_heard_nothing_is_forgotten_once_a_bulb_answers_again() -> None:
    """Back on the network, the lamp switched off, and the page still said no Kasa device answered."""
    lamp = FakeBulb("10.0.0.36", "Desk lamp")
    network = FakeNetwork(lamp)
    devices = _devices(network)
    [entry] = await devices.devices()
    lamp.down, network.bulbs, network.diagnostic = True, {}, kasa_devices._NOTHING_FOUND
    with pytest.raises(kasa_devices.NotAnswering):
        await devices.apply(entry["id"], LightChange(on=True))
    away = devices.problem
    lamp.down = False  # the old connection answers again

    await devices.apply(entry["id"], LightChange(on=False))

    assert away.startswith("No Kasa device answers") and devices.problem == ""


async def test_bulbs_that_stop_answering_together_share_one_look() -> None:
    lamp, porch = FakeBulb("10.0.0.36", "Desk lamp"), FakeBulb("10.0.0.40", "Porch light")
    network = FakeNetwork(lamp, porch)
    devices = _devices(network)
    entries = await devices.devices()
    lamp.down = porch.down = True
    network.bulbs = {"10.0.0.52": FakeBulb("10.0.0.52", "Desk lamp"), "10.0.0.53": FakeBulb("10.0.0.53", "Porch light")}

    states = await asyncio.gather(*(devices.apply(entry["id"], LightChange(on=False)) for entry in entries))

    assert [s["host"] for s in states] == ["10.0.0.52", "10.0.0.53"] and network.discoveries == 2


async def test_done_is_what_the_device_reports() -> None:
    lamp = FakeBulb("10.0.0.36", "Desk lamp")
    devices = _devices(FakeNetwork(lamp))
    [entry] = await devices.devices()

    async def ignores(state):  # the bulb answers, and changes nothing
        return {}

    lamp.light.set_state = ignores
    with pytest.raises(RuntimeError, match="^did not confirm the change$"):  # the page and chat name the lamp
        await devices.apply(entry["id"], LightChange(brightness=70))


async def test_bulbs_that_refuse_the_login_are_named_once_with_where_to_add_it(monkeypatch) -> None:
    """Four bulbs each repeated python-kasa's long error, then a hint to check the Wi-Fi, though all were found."""
    from kasa.exceptions import AuthenticationError

    network = FakeNetwork(FakeBulb("10.0.0.36", "KL125(US)"), FakeBulb("10.0.0.47", "KL125(US) "))
    for host in network.bulbs:
        network.refuse[host] = AuthenticationError("Device response did not match our challenge")
    devices = _devices(network)

    assert await devices.devices() == []
    assert devices.locked == ["KL125(US) (10.0.0.36)", "KL125(US)  (10.0.0.47)"]
    assert devices.problem.startswith("2 bulbs need the TP-Link login they were set up with in the Kasa app")
    assert devices.problem.count("challenge") == 0 and "Settings, Secrets, as kasa_email" in devices.problem


async def test_a_bulb_that_will_not_answer_for_another_reason_is_named_with_why() -> None:
    network = FakeNetwork(FakeBulb("10.0.0.36", "Desk lamp"))
    network.refuse["10.0.0.36"] = TimeoutError("timed out")
    devices = _devices(network)

    assert await devices.devices() == [] and devices.locked == []
    assert devices.problem == "Not answering: Desk lamp (10.0.0.36): TimeoutError: timed out"


async def test_each_login_is_tried_until_a_bulb_accepts_one(monkeypatch) -> None:
    """A bulb answers only the account it was set up with: yours, or a friend's."""
    from kasa import Discover
    from kasa.exceptions import AuthenticationError

    store = secret_store.store()
    for name, value in (
        ("kasa_email", "me@example.com"),
        ("kasa_password", "mine-123"),
        ("kasa_second_email", "friend@example.com"),
        ("kasa_second_password", "theirs"),
    ):
        store.save(name, value)
    tried: list[str] = []

    async def discover_single(host, *, timeout, username, password):
        tried.append(username)
        if username != "friend@example.com":
            raise AuthenticationError("Device response did not match our challenge")
        return FakeBulb(host, "Sofa light")

    monkeypatch.setattr(Discover, "discover_single", discover_single)

    bulb = await kasa_devices.open_device_at("10.0.0.161")

    assert tried == ["me@example.com", "friend@example.com"] and bulb.alias == "Sofa light"


async def test_every_connection_is_released_when_north_stops() -> None:
    bulbs = FakeBulb("10.0.0.36", "Desk lamp"), FakeBulb("10.0.0.47", "Floor lamp")
    devices = _devices(FakeNetwork(*bulbs))
    await devices.devices()

    await devices.aclose()

    assert all(bulb.closed for bulb in bulbs)


async def test_a_bulb_that_logs_in_the_newer_way_is_answered_that_way() -> None:
    """Kasa bulbs of the older kind (KL125, KL135) on newer firmware announce login version 2.
    python-kasa 0.10 answered them with the version 1 handshake, which they refuse; 0.11 fixed it
    (python-kasa #1731), and pyproject.toml asks for 0.12 so an install never goes back."""
    from kasa import DeviceConfig, DeviceConnectionParameters, DeviceEncryptionType, DeviceFamily
    from kasa.device_factory import get_protocol

    newer = DeviceConnectionParameters(DeviceFamily.IotSmartBulb, DeviceEncryptionType.Klap, login_version=2)
    protocol = get_protocol(DeviceConfig(host="10.0.0.47", connection_type=newer))
    try:
        assert type(protocol._transport).__name__ == "KlapTransportV2"
    finally:
        await protocol.close()
