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


async def test_a_tp_link_login_saved_here_unlocks_the_bulbs_set_up_with_it(home, monkeypatch, tmp_path) -> None:
    """The user had no login set: all four bulbs refused, and the only fix was editing north's .env."""
    import os
    from pathlib import Path

    from kasa.exceptions import AuthenticationError

    from tools.specialized import kasa_devices

    services, bulbs, _ = home
    for key in (
        "NORTH_KASA_USERNAME",
        "NORTH_KASA_PASSWORD",
        "NORTH_KASA_SECOND_USERNAME",
        "NORTH_KASA_SECOND_PASSWORD",
    ):
        monkeypatch.setenv(key, "")  # restored afterwards, whatever the routes write
    north_home = Path(os.environ["NORTH_HOME"])
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
    services = services.replace(north_home=north_home)
    with bind_services(services):
        before = await lights.list_lights()
        mine = await lights.save_kasa_account(
            "yours", lights.KasaAccountUpdate(email="me@example.com", password="p@ss w#rd")
        )
        both = await lights.save_kasa_account(
            "second", lights.KasaAccountUpdate(email="friend@example.com", password="theirs")
        )
        listed = await lights.kasa_accounts()
        for slot in ("yours", "second"):
            await lights.remove_kasa_account(slot)

    assert before["lights"] == [] and len(before["locked"]) == 4
    assert mine["answering"] == 2 and len(mine["locked"]) == 2 and both["answering"] == 4 and both["locked"] == []
    assert listed["accounts"] == [
        {"slot": "yours", "email": "me@example.com", "saved": True},
        {"slot": "second", "email": "friend@example.com", "saved": True},
    ]
    assert "p@ss" not in str(listed) and "theirs" not in str(both)  # a password is never sent back
    assert "NORTH_KASA" not in (north_home / ".env").read_text()


async def test_a_password_the_file_cannot_hold_is_refused_before_anything_is_saved(home) -> None:
    services, _, _ = home
    with bind_services(services), pytest.raises(HTTPException) as refused:
        await lights.save_kasa_account("yours", lights.KasaAccountUpdate(email="me@example.com", password="x${HOME}"))

    assert refused.value.status_code == 422 and not (services.north_home / ".env").exists()


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
