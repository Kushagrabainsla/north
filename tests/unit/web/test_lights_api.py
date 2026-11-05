"""The Lights page's routes: your Kasa devices, controlled by you, with no approval card (CODING_STYLE §7.3).

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
        north_settings=settings, web_runtime=WebRuntime(), north_home=tmp_path, tool_registry=registry
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
        await lights.music_frames(
            lights.LiveFrames(frames=[lights.LiveFrame(id=desk_id, hue=200, saturation=90, brightness=80)])
        )
        await asyncio.sleep(0.05)
        during = desk.light.hsv.hue
        await lights.stop_music_sync()

    assert not_started.value.status_code == 409 and following == {"following": [desk_id]}
    assert during == 200 and (desk.is_on, desk.light.brightness, desk.light.color_temp) == (True, 40, 2700)
    assert store.all() == []
