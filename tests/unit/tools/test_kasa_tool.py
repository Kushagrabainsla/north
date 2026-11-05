"""The kasa tool: what you ask for from chat, carried out through `KasaDevices`.

Reaching the devices is tested in test_kasa_devices.py; this is the tool's own part,
reading the request and saying what happened.
"""

from __future__ import annotations

import pytest

from tests.unit.tools.kasa_fakes import FakeBulb, FakeNetwork
from tools.models import ToolInput
from tools.specialized import kasa_tool
from tools.specialized.kasa_devices import KasaDevices


def _tool(network: FakeNetwork) -> kasa_tool.KasaTool:
    return kasa_tool.KasaTool(KasaDevices(discover=network.discover, open_device=network.open))


def test_action_aliases_cover_natural_tool_calls() -> None:
    assert kasa_tool._ACTION_ALIASES["set_brightness"] == "brightness"
    assert kasa_tool._ACTION_ALIASES["turn_on"] == "on"
    assert kasa_tool._ACTION_ALIASES["apply_scene"] == "scene"


def test_every_alias_lands_on_a_real_action() -> None:
    """The alias table and the action table cannot drift apart unnoticed."""
    assert set(kasa_tool._ACTION_ALIASES.values()) <= kasa_tool._KNOWN_ACTIONS


async def test_a_discovery_failure_is_a_tool_failure() -> None:
    out = await _tool(FakeNetwork(diagnostic="kasa discover exited 1: permission denied")).run(
        ToolInput(params={"action": "list"})
    )

    assert out.success is False and "permission denied" in (out.error or "")


async def test_devices_found_but_unreachable_are_a_failure_that_says_why() -> None:
    network = FakeNetwork(FakeBulb("192.168.1.20", "Desk lamp"))
    network.refuse["192.168.1.20"] = TimeoutError("timed out")

    out = await _tool(network).run(ToolInput(params={"action": "list"}))

    assert out.success is False and "Desk lamp (192.168.1.20): TimeoutError" in (out.error or "")


async def test_the_moody_scene_sets_brightness_and_warmth() -> None:
    lamp = FakeBulb("10.0.0.1", "Desk lamp")

    out = await _tool(FakeNetwork(lamp)).run(ToolInput(params={"action": "scene", "scene": "moody"}))

    [state] = out.data["devices"]
    assert out.success and (state["brightness"], state["kelvin"]) == (30, 2700)


async def test_a_scene_says_when_a_device_cannot_do_it() -> None:
    out = await _tool(FakeNetwork(FakeBulb("10.0.0.2", "Coffee plug", plug=True))).run(
        ToolInput(params={"action": "scene", "scene": "party"})
    )

    assert out.success is False and "only be switched on and off" in (out.error or "")


async def test_list_does_not_need_a_device() -> None:
    """The action the "device is required" error tells you to run must not need one.

    It did. `list` was not exempt from the target check, so the only route out of
    that error was the error itself.
    """
    out = await _tool(FakeNetwork(FakeBulb("10.0.0.36", "Desk lamp"))).run(ToolInput(params={"action": "list"}))

    assert out.success and [d["name"] for d in out.data["devices"]] == ["Desk lamp"]


async def test_a_control_action_still_needs_a_device() -> None:
    """The guard exists so a command can never fan out across the whole house."""
    out = await kasa_tool.KasaTool().run(ToolInput(params={"action": "off"}))
    assert out.success is False
    assert "device' is required" in (out.error or "")


async def test_a_device_is_found_by_its_name_whatever_its_case() -> None:
    desk, floor = FakeBulb("10.0.0.36", "Desk lamp"), FakeBulb("10.0.0.47", "Floor lamp")

    out = await _tool(FakeNetwork(desk, floor)).run(ToolInput(params={"action": "on", "device": "desk LAMP"}))

    assert out.success and desk.is_on and not floor.is_on
    assert out.data["message"] == "Turned on: Desk lamp."


async def test_devices_stay_connected_between_commands_and_close_when_north_stops() -> None:
    """Each command used to search the network and connect again, 2-5 s every time."""
    lamp = FakeBulb("10.0.0.36", "Desk lamp")
    network = FakeNetwork(lamp)
    tool = _tool(network)

    await tool.run(ToolInput(params={"action": "on", "device": "desk lamp"}))
    await tool.run(ToolInput(params={"action": "off", "device": "desk lamp"}))
    await tool.aclose()

    assert network.discoveries == 1 and network.opened == ["10.0.0.36"] and lamp.closed


async def test_dim_is_the_word_people_use() -> None:
    """ "dim my lights to 50%" reached the tool as an undefined action and was
    reported as a missing device, which blamed the request for the vocabulary."""
    lamp = FakeBulb("10.0.0.36", "Desk lamp")

    out = await _tool(FakeNetwork(lamp)).run(ToolInput(params={"action": "dim", "brightness": 50}))

    assert out.success and lamp.light.brightness == 50


async def test_an_unknown_action_says_it_is_unknown() -> None:
    """It used to answer "device is required" for every word nobody had defined."""
    out = await kasa_tool.KasaTool().run(ToolInput(params={"action": "banana"}))

    assert out.success is False
    assert "Unknown action" in (out.error or "")
    assert "device" not in (out.error or "").split("Valid actions")[0]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"action": "color", "color": "blue"}, {"hue": 240, "saturation": 100}),
        ({"action": "color_temp", "color_temp": "warm"}, {"kelvin": 2700}),
        ({"action": "brightness", "brightness": 0}, {"on": False}),
    ],
)
async def test_named_colours_warmth_and_zero_brightness(params: dict, expected: dict) -> None:
    lamp = FakeBulb("10.0.0.36", "Desk lamp")
    lamp.is_on = True

    out = await _tool(FakeNetwork(lamp)).run(ToolInput(params={**params, "device": "desk lamp"}))

    [state] = out.data["devices"]
    assert out.success and {key: state[key] for key in expected} == expected
