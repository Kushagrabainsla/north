"""The Devices page's routes: your devices of every brand, controlled by you, with no approval card (§7.3).

The user has two bulbs of their own and two of a friend's on the same network, and
wants to control only theirs, or all of them.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from approval.store import ApprovalStore
from config.approval_mode import ApprovalMode
from config.strategy import NorthSettings
from orchestrator.api_context import ApiServices, bind_services
from tests.conftest import approvals
from tests.unit.tools.kasa_fakes import FakeBulb, FakeNetwork
from tools.home import Home
from tools.registry import ToolRegistry
from tools.specialized.kasa_devices import KasaDevices
from tools.specialized.kasa_tool import KasaTool
from web import lights
from web.api import WebRuntime

MINE = ("Desk lamp", "Bed lamp")
FRIENDS = ("Sofa light", "Hall light")


@pytest.fixture
def home(tmp_path):
    bulbs = {name: FakeBulb(f"10.0.0.{i}", name) for i, name in enumerate((*MINE, *FRIENDS), start=10)}
    network = FakeNetwork(*bulbs.values())
    store = ApprovalStore()
    registry = ToolRegistry(auto_register=False, approvals=approvals(ApprovalMode.ASK, store=store))
    registry.register(KasaTool(KasaDevices(discover=network.discover, open_device=network.open)))
    settings = NorthSettings(tmp_path / "settings.json")
    services = ApiServices(
        north_settings=settings,
        web_runtime=WebRuntime(),
        north_home=tmp_path,
        tool_registry=registry,
        home=Home(registry),
    )
    return services, bulbs, store


def _ids(found: dict, names) -> list[str]:
    return [light["id"] for light in found["lights"] if light["name"] in names]


async def test_a_change_you_make_raises_no_card_even_in_ask_mode(home) -> None:
    services, bulbs, store = home
    with bind_services(services):
        found = await lights.list_lights()
        changed = await lights.apply_lights(lights.LightsApply(ids=_ids(found, ["Desk lamp"]), on=True, brightness=40))

    [state] = changed["lights"]
    assert state["on"] and state["brightness"] == 40 and bulbs["Desk lamp"].is_on
    assert store.all() == []  # nothing was asked: the person deciding is you


async def test_your_lights_are_remembered_and_my_lights_leaves_a_friends_alone(home, tmp_path) -> None:
    services, bulbs, _ = home
    with bind_services(services):
        found = await lights.list_lights()
        for device_id in _ids(found, MINE):
            await lights.mark_mine(device_id, lights.MineUpdate(mine=True))
        listed = await lights.list_lights()
        mine = [light["id"] for light in listed["lights"] if light["mine"]]
        await lights.apply_lights(lights.LightsApply(ids=mine, on=True))

    assert sorted(light["name"] for light in listed["lights"] if light["mine"]) == sorted(MINE)
    assert [name for name, bulb in bulbs.items() if bulb.is_on] == list(MINE)
    assert sorted(NorthSettings(tmp_path / "settings.json").my_lights) == sorted(mine)  # kept across restarts


async def test_a_scene_for_all_lights_reaches_every_one(home) -> None:
    services, bulbs, _ = home
    with bind_services(services):
        found = await lights.list_lights()
        changed = await lights.apply_lights(lights.LightsApply(ids=_ids(found, (*MINE, *FRIENDS)), scene="movie"))

    assert len(changed["lights"]) == 4 and changed["errors"] == []
    assert all((bulb.light.brightness, bulb.light.color_temp) == (15, 2500) for bulb in bulbs.values())
    assert "movie" in found["scenes"]


async def test_a_light_that_will_not_answer_is_named_and_the_rest_still_change(home) -> None:
    services, bulbs, _ = home
    with bind_services(services):
        found = await lights.list_lights()
        bulbs["Bed lamp"].light.set_state = _refuses
        changed = await lights.apply_lights(lights.LightsApply(ids=_ids(found, MINE), on=True))
        with pytest.raises(HTTPException) as nobody:
            await lights.apply_lights(lights.LightsApply(ids=_ids(found, ["Bed lamp"]), on=True))

    assert [light["name"] for light in changed["lights"]] == ["Desk lamp"]
    assert "unplugged" in changed["errors"][0]["error"] and nobody.value.status_code == 502


@pytest.mark.parametrize(
    "body",
    [
        {"ids": ["a"]},  # nothing to change
        {"ids": ["a"], "scene": "movie", "on": True},  # a scene goes on its own
        {"ids": ["a"], "scene": "disco"},
        {"ids": ["a"], "hue": 120},  # a colour needs its saturation
        {"ids": ["a"], "brightness": 0},  # off is `on: false`
    ],
)
def test_a_change_that_makes_no_sense_is_refused(body: dict) -> None:
    with pytest.raises(ValidationError):
        lights.LightsApply(**body)


async def _refuses(state):
    raise OSError("unplugged")


async def test_music_sync_follows_the_frames_then_puts_the_lights_back(home) -> None:
    import asyncio

    services, bulbs, store = home
    desk = bulbs["Desk lamp"]
    desk.is_on, desk.light.brightness, desk.light.color_temp = True, 40, 2700
    with bind_services(services):
        found = await lights.list_lights()
        [desk_id] = _ids(found, ["Desk lamp"])
        with pytest.raises(HTTPException) as not_started:
            await lights.music_frames(
                lights.LiveFrames(frames=[lights.LiveFrame(id=desk_id, hue=1, saturation=1, brightness=1)])
            )
        following = await lights.start_music_sync(lights.LiveStart(ids=[desk_id]))
        await lights.music_frames(  # a beat's frame, which the ones after it do not overtake
            lights.LiveFrames(frames=[lights.LiveFrame(id=desk_id, hue=200, saturation=90, brightness=80)], beat=True)
        )
        await asyncio.sleep(0.05)
        during = desk.light.hsv.hue
        await lights.stop_music_sync()

    assert not_started.value.status_code == 409 and following == {"following": [desk_id]}
    assert during == 200 and (desk.is_on, desk.light.brightness, desk.light.color_temp) == (True, 40, 2700)
    assert store.all() == []


async def test_tp_link_logins_saved_as_secrets_unlock_the_bulbs_set_up_with_them(home) -> None:
    """The logins had a panel of their own and lived in .env; now they are secrets, beside every other key."""
    from kasa.exceptions import AuthenticationError

    from config import secret_store
    from tools.specialized import kasa_devices

    services, bulbs, _ = home
    owners = dict.fromkeys(MINE, ("me@example.com", "p@ss w#rd")) | dict.fromkeys(
        FRIENDS, ("friend@example.com", "theirs")
    )
    network = services.tool_registry.get("kasa").devices
    opened = network._open

    async def needs_its_owners_login(host):
        bulb = next(b for b in bulbs.values() if b.host == host)
        if owners[bulb.alias] not in kasa_devices.accounts():
            raise AuthenticationError("Device response did not match our challenge")
        return await opened(host)

    network._open = needs_its_owners_login
    store = secret_store.store()
    with bind_services(services):
        before = await lights.list_lights()
        store.save("kasa_email", "me@example.com")
        store.save("kasa_password", "p@ss w#rd")
        mine = await lights.list_lights(look_again=True)
        store.save("kasa_second_email", "friend@example.com")
        store.save("kasa_second_password", "theirs")
        both = await lights.list_lights(look_again=True)

    assert before["lights"] == [] and len(before["locked"]) == 4
    assert "Settings, Secrets, as kasa_email and kasa_password" in before["problem"]
    assert len(mine["lights"]) == 2 and len(mine["locked"]) == 2
    assert len(both["lights"]) == 4 and both["locked"] == []


def test_logins_kept_in_env_move_to_secrets_once_and_leave_env(tmp_path) -> None:
    """The user's TP-Link logins were in north's .env; on the first start they become secrets."""
    from config import secret_store
    from config.env_file import write_env_key
    from tools.specialized.kasa_devices import move_logins_to_secrets

    write_env_key(tmp_path, "NORTH_KASA_USERNAME", "me@example.com")
    write_env_key(tmp_path, "NORTH_KASA_PASSWORD", "p@ss w#rd")
    write_env_key(tmp_path, "NORTH_KASA_SECOND_USERNAME", "friend@example.com")
    write_env_key(tmp_path, "NORTH_KASA_SECOND_PASSWORD", "abc")  # too short to keep: stays, and is named
    write_env_key(tmp_path, "NORTH_GROQ_API_KEY", "gsk_untouched")

    moved = move_logins_to_secrets(tmp_path)
    again = move_logins_to_secrets(tmp_path)

    store = secret_store.SecretStore(tmp_path)
    assert moved == ["kasa_email", "kasa_password", "kasa_second_email"] and again == []
    assert (store.value("kasa_email"), store.value("kasa_password")) == ("me@example.com", "p@ss w#rd")
    assert [s.note for s in store.saved() if s.name == "kasa_email"] == ["TP-Link login (email) for your Kasa bulbs"]
    env = (tmp_path / ".env").read_text()
    assert "NORTH_KASA_USERNAME" not in env and "NORTH_KASA_SECOND_PASSWORD=abc" in env and "gsk_untouched" in env


async def test_a_brand_added_through_north_shows_here_and_is_yours_to_mark_and_change(home) -> None:
    """Only Kasa bulbs could show here; a Hue lamp north wrote a tool for now sits beside them."""
    from fastapi.routing import APIRoute

    from tests.unit.tools.test_home import _Hue

    services, _, store = home
    hue = _Hue()
    services.tool_registry.register(hue)
    with bind_services(services):
        found = await lights.list_lights()
        await lights.mark_mine("hue_lights/3", lights.MineUpdate(mine=True))
        changed = await lights.apply_lights(lights.LightsApply(ids=["hue_lights/3"], scene="focus"))
        mine = {light["id"] for light in (await lights.list_lights())["lights"] if light["mine"]}

    assert {"Hall lamp", "Kettle plug"} <= {light["name"] for light in found["lights"]}
    assert mine == {"hue_lights/3"}
    [lamp] = changed["lights"]
    assert (lamp["source"], lamp["brightness"], lamp["kelvin"]) == ("hue_lights", 90, 5000)
    assert store.all() == []  # yours to change, with no card
    [route] = [r for r in lights.router.routes if isinstance(r, APIRoute) and r.path.endswith("/mine")]
    assert route.matches({"type": "http", "path": "/lights/hue_lights/3/mine", "method": "PUT"})[0].name == "FULL"


async def test_find_devices_lists_every_device_and_marks_the_kasa_lamps_already_here(home) -> None:
    """The user's two Kasa lamps were left out of Find devices with nothing saying why."""
    from tools.home_discovery import HOMEKIT, WIZ, Found
    from tools.home_network import NetworkDevice

    services, bulbs, _ = home
    desk = bulbs["Desk lamp"]

    async def on_the_network() -> list[NetworkDevice]:
        return [
            NetworkDevice("192.168.1.60", maker="WiZ", found=Found(WIZ, "WiZ light", "192.168.1.60", "WiZ discovery")),
            NetworkDevice(desk.host, mac="50:c7:bf:00:00:01", maker="TP-Link"),
            NetworkDevice("10.0.0.223", found=Found(HOMEKIT, "TSTAT-276BCD", "10.0.0.223", "mDNS _hap._tcp", "bridge")),
            NetworkDevice("10.0.0.55", mac="a4:cf:12:aa:bb:cc", maker="Espressif"),
            NetworkDevice("10.0.0.1", role="your router"),
        ]

    services = services.replace(home=Home(services.tool_registry, find_on_network=on_the_network))
    with bind_services(services):
        devices = {d["address"]: d for d in (await lights.discover_devices())["devices"]}

    assert devices["192.168.1.60"]["connect"] == "add"
    assert devices[desk.host]["connect"] == "here" and devices[desk.host]["name"] == "Desk lamp"
    assert devices["10.0.0.223"]["connect"] == "try"  # north cannot pair HomeKit, but may still look
    assert devices["10.0.0.55"]["connect"] == "try" and devices["10.0.0.55"]["hint"].startswith("Espressif makes")
    assert devices["10.0.0.1"]["connect"] == "none"
