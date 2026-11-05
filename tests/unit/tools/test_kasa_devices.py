"""Your Kasa devices, found once and kept connected: the one place north reaches them.

Every command used to search the network, connect, act and disconnect, 2-5 s each:
too slow for a slider on the dashboard and far too slow to follow music.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

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
    assert "NORTH_KASA_USERNAME" in diagnostic


def test_the_login_is_passed_to_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    from config.settings import settings

    monkeypatch.setattr(settings, "kasa_username", "me@example.com", raising=False)
    monkeypatch.setattr(settings, "kasa_password", "hunter2", raising=False)
    assert kasa_devices.credential_args() == ["--username", "me@example.com", "--password", "hunter2"]


def test_no_login_configured_passes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from config.settings import settings

    monkeypatch.setattr(settings, "kasa_username", "", raising=False)
    monkeypatch.setattr(settings, "kasa_password", "", raising=False)
    assert kasa_devices.credential_args() == []


def test_the_password_never_reaches_an_error_message() -> None:
    """Diagnostics quote the command they ran, and one of its arguments is a secret."""
    redacted = kasa_devices.redacted(["kasa", "--username", "me@example.com", "--password", "hunter2", "discover"])
    assert "hunter2" not in redacted
    assert redacted[4] == "***"
    assert "me@example.com" in redacted  # only the password is a secret


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


async def test_done_is_what_the_device_reports() -> None:
    lamp = FakeBulb("10.0.0.36", "Desk lamp")
    devices = _devices(FakeNetwork(lamp))
    [entry] = await devices.devices()

    async def ignores(state):  # the bulb answers, and changes nothing
        return {}

    lamp.light.set_state = ignores
    with pytest.raises(RuntimeError, match="did not confirm"):
        await devices.apply(entry["id"], LightChange(brightness=70))


async def test_a_device_that_will_not_connect_is_named_with_why(monkeypatch: pytest.MonkeyPatch) -> None:
    from config.settings import settings

    monkeypatch.setattr(settings, "kasa_username", "", raising=False)
    monkeypatch.setattr(settings, "kasa_password", "", raising=False)
    network = FakeNetwork(FakeBulb("10.0.0.36", "Desk lamp"))
    network.refuse["10.0.0.36"] = type("AuthenticationError", (Exception,), {})("refused")
    devices = _devices(network)

    assert await devices.devices() == []
    assert "Desk lamp (10.0.0.36): AuthenticationError" in devices.problem
    assert "NORTH_KASA_USERNAME" in devices.problem


async def test_every_connection_is_released_when_north_stops() -> None:
    bulbs = FakeBulb("10.0.0.36", "Desk lamp"), FakeBulb("10.0.0.47", "Floor lamp")
    devices = _devices(FakeNetwork(*bulbs))
    await devices.devices()

    await devices.aclose()

    assert all(bulb.closed for bulb in bulbs)
