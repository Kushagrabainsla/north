"""Your Kasa devices, found once and kept connected: the one place north reaches them.

Every command used to search the network, connect, act and disconnect, 2-5 s each:
too slow for a slider on the dashboard and far too slow to follow music.
"""

from __future__ import annotations

import asyncio
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


async def test_bulbs_that_refuse_the_login_are_named_once_with_where_to_add_it(monkeypatch) -> None:
    """Four bulbs each repeated python-kasa's long error, then a hint to check the Wi-Fi, though all were found."""
    from kasa.exceptions import AuthenticationError

    from config.settings import settings

    for field in ("kasa_username", "kasa_password", "kasa_second_username", "kasa_second_password"):
        monkeypatch.setattr(settings, field, "", raising=False)
    network = FakeNetwork(FakeBulb("10.0.0.36", "KL125(US)"), FakeBulb("10.0.0.47", "KL125(US) "))
    for host in network.bulbs:
        network.refuse[host] = AuthenticationError("Device response did not match our challenge")
    devices = _devices(network)

    assert await devices.devices() == []
    assert devices.locked == ["KL125(US) (10.0.0.36)", "KL125(US)  (10.0.0.47)"]
    assert devices.problem.startswith("2 bulbs need the TP-Link login they were set up with in the Kasa app")
    assert devices.problem.count("challenge") == 0 and "Settings > Connections > Kasa lights" in devices.problem


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

    from config.settings import settings

    monkeypatch.setattr(settings, "kasa_username", "me@example.com", raising=False)
    monkeypatch.setattr(settings, "kasa_password", "mine", raising=False)
    monkeypatch.setattr(settings, "kasa_second_username", "friend@example.com", raising=False)
    monkeypatch.setattr(settings, "kasa_second_password", "theirs", raising=False)
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


async def _settle(seconds: float = 0.05) -> None:
    await asyncio.sleep(seconds)


async def test_a_slow_bulb_gets_the_newest_frame_and_never_falls_behind() -> None:
    lamp = FakeBulb("10.0.0.36", "Desk lamp")
    devices = _devices(FakeNetwork(lamp))
    [entry] = await devices.devices()
    quick = lamp.light.set_state

    async def slow(state):
        await asyncio.sleep(0.05)
        return await quick(state)

    lamp.light.set_state = slow
    await devices.live.start([entry["id"]])
    for hue in (10, 20, 30, 40, 50):
        devices.live.frame({entry["id"]: LightChange(on=True, hue=hue, saturation=100, brightness=60)})
        if hue == 10:
            await _settle(0.01)  # the first is on its way; the rest arrive while the bulb is busy
    await _settle(0.2)

    assert [state.hue for state in lamp.light.sent] == [10, 50]  # 20, 30 and 40 were overtaken


async def test_a_slow_bulb_fades_for_as_long_as_it_takes_between_frames() -> None:
    """A short fade on a bulb that took longer between changes jumped, held, and jumped: a flicker."""
    lamp = FakeBulb("10.0.0.36", "Desk lamp")
    devices = _devices(FakeNetwork(lamp))
    [entry] = await devices.devices()
    fades: list[int | None] = []
    apply = devices.apply

    async def slow(device_id: str, change: LightChange, *, confirm: bool = True) -> dict:
        fades.append(change.transition_ms)
        await asyncio.sleep(0.08)
        return await apply(device_id, change, confirm=confirm)

    devices.apply = slow  # type: ignore[method-assign]
    await devices.live.start([entry["id"]])
    for hue in (10, 20, 30):
        devices.live.frame(
            {entry["id"]: LightChange(on=True, hue=hue, saturation=100, brightness=60, transition_ms=20)}
        )
        await _settle(0.03)
    await _settle(0.2)

    assert fades[0] == 20  # nothing to go by yet: the fade asked for
    assert len(fades) == 2 and 70 <= (fades[1] or 0) <= kasa_devices._LONGEST_FADE_MS


async def test_stopping_puts_every_light_back_as_it_was() -> None:
    lit, dark = FakeBulb("10.0.0.36", "Desk lamp"), FakeBulb("10.0.0.47", "Bed lamp")
    lit.is_on, lit.light.brightness, lit.light.color_temp = True, 40, 2700
    devices = _devices(FakeNetwork(lit, dark))
    ids = [d["id"] for d in await devices.devices()]

    await devices.live.start(ids)
    devices.live.frame({i: LightChange(on=True, hue=200, saturation=100, brightness=90) for i in ids})
    await _settle()
    await devices.live.stop()

    assert (lit.is_on, lit.light.brightness, lit.light.color_temp) == (True, 40, 2700)
    assert dark.is_on is False and devices.live.following == []


async def test_lights_are_put_back_by_themselves_when_frames_stop_coming() -> None:
    """The tab was closed, or the computer slept: nobody pressed Stop."""
    lamp = FakeBulb("10.0.0.36", "Desk lamp")
    devices = _devices(FakeNetwork(lamp))
    devices.live = kasa_devices.LiveLights(devices, idle_seconds=0.1)
    [entry] = await devices.devices()

    await devices.live.start([entry["id"]])
    devices.live.frame({entry["id"]: LightChange(on=True, hue=200, saturation=100, brightness=90)})
    await _settle(0.4)

    assert devices.live.following == [] and lamp.is_on is False


async def test_only_the_lights_you_chose_follow_the_music() -> None:
    mine, friends = FakeBulb("10.0.0.36", "Desk lamp"), FakeBulb("10.0.0.47", "Sofa light")
    devices = _devices(FakeNetwork(mine, friends))
    found = {d["name"]: d["id"] for d in await devices.devices()}

    with pytest.raises(LookupError):  # nothing was started
        devices.live.frame({found["Desk lamp"]: LightChange(on=True, hue=1, saturation=1, brightness=1)})
    await devices.live.start([found["Desk lamp"]])
    devices.live.frame({i: LightChange(on=True, hue=120, saturation=100, brightness=80) for i in found.values()})
    await _settle()

    assert mine.light.sent and friends.light.sent == []
