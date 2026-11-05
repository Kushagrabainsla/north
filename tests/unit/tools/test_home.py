"""Your home devices from every brand, in one list, with the same controls and music sync.

Only Kasa bulbs showed on the Lights page, and only Kasa could follow music. Now any
brand north writes a `DeviceTool` for (when you add a device on the Devices page) shows
beside them, and music sync streams to whichever lights you chose, of any brand.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from config import secret_store
from config.approval_mode import ApprovalMode
from config.secret_store import secret
from tests.conftest import approvals
from tests.unit.tools.kasa_fakes import FakeBulb, FakeNetwork
from tools import home as home_module
from tools.home import SCENES, Device, DeviceTool, Home, LightChange
from tools.models import ToolInput
from tools.registry import LearnedTool, ToolRegistry
from tools.specialized.kasa_devices import KasaDevices
from tools.specialized.kasa_tool import KasaTool


class _Hue(DeviceTool):
    """A brand north wrote a tool for: one colour lamp and one plug behind a bridge."""

    name = "hue_lights"
    description = "Philips Hue lights on the bridge in the hall"

    def __init__(self) -> None:
        self.devices = {
            "3": Device(
                "3", "Hall lamp", on=False, dims=True, colors=True, white=(2000, 6500), brightness=50, kelvin=2700
            ),
            "7": Device("7", "Kettle plug", on=True),
        }
        self.changes: list[tuple[str, LightChange]] = []
        self.answering = True

    async def find(self) -> list[Device]:
        if not self.answering:
            raise ConnectionError("the bridge at 192.168.1.20 did not answer")
        return list(self.devices.values())

    async def change(self, device_id: str, change: LightChange) -> Device:
        self.changes.append((device_id, change))
        device = self.devices[device_id]
        if change.on is not None:
            device = replace(device, on=change.on)
        if change.brightness is not None:
            device = replace(device, on=True, brightness=change.brightness)
        if change.hue is not None:
            device = replace(device, on=True, hue=change.hue, saturation=change.saturation, kelvin=None)
        if change.kelvin is not None:
            device = replace(device, on=True, kelvin=change.kelvin, hue=None, saturation=None)
        self.devices[device_id] = device
        return device


def _home(network: FakeNetwork | None = None, *brands: DeviceTool, idle_seconds: float = 5.0) -> Home:
    registry = ToolRegistry(auto_register=False)
    if network is not None:
        registry.register(KasaTool(KasaDevices(discover=network.discover, open_device=network.open)))
    for brand in brands:
        registry.register(brand)
    return Home(registry, idle_seconds=idle_seconds)


async def _settle(seconds: float = 0.05) -> None:
    await asyncio.sleep(seconds)


async def test_a_slow_bulb_gets_the_newest_frame_and_never_falls_behind() -> None:
    lamp = FakeBulb("10.0.0.36", "Desk lamp")
    home = _home(FakeNetwork(lamp))
    [entry] = await home.devices()
    quick = lamp.light.set_state

    async def slow(state):
        await asyncio.sleep(0.05)
        return await quick(state)

    lamp.light.set_state = slow
    await home.live.start([entry["id"]])
    for hue in (10, 20, 30, 40, 50):
        home.live.frame({entry["id"]: LightChange(on=True, hue=hue, saturation=100, brightness=60)})
        if hue == 10:
            await _settle(0.01)  # the first is on its way; the rest arrive while the bulb is busy
    await _settle(0.2)

    assert [state.hue for state in lamp.light.sent] == [10, 50]  # 20, 30 and 40 were overtaken


async def test_a_beat_is_not_overtaken_by_the_fading_after_it() -> None:
    """On a slow bulb the newest frame wins; a beat's flash must still reach it, or the lights miss the beat."""
    lamp = FakeBulb("10.0.0.36", "Desk lamp")
    home = _home(FakeNetwork(lamp))
    [entry] = await home.devices()
    quick = lamp.light.set_state

    async def slow(state):
        await asyncio.sleep(0.05)
        return await quick(state)

    lamp.light.set_state = slow
    await home.live.start([entry["id"]])
    home.live.frame({entry["id"]: LightChange(on=True, hue=10, saturation=100, brightness=30)})
    await _settle(0.01)  # on its way; what follows arrives while the bulb is busy
    home.live.frame({entry["id"]: LightChange(on=True, hue=20, saturation=100, brightness=95)}, beat=True)
    for hue in (30, 40):  # the flash fading
        home.live.frame({entry["id"]: LightChange(on=True, hue=hue, saturation=100, brightness=60)})
    await _settle(0.2)

    assert [state.hue for state in lamp.light.sent] == [10, 20]


async def test_a_beat_keeps_its_quick_rise_on_a_slow_bulb() -> None:
    """Stretched to the bulb's pace, a beat's rise blurred into the fading and no flash showed."""
    lamp = FakeBulb("10.0.0.36", "Desk lamp")
    home = _home(FakeNetwork(lamp))
    [entry] = await home.devices()
    fades: list[int | None] = []
    apply = home.apply

    async def slow(device_id: str, change: LightChange, *, confirm: bool = True) -> dict:
        fades.append(change.transition_ms)
        await asyncio.sleep(0.08)
        return await apply(device_id, change, confirm=confirm)

    home.apply = slow  # type: ignore[method-assign]
    await home.live.start([entry["id"]])
    home.live.frame({entry["id"]: LightChange(on=True, hue=10, saturation=100, brightness=30, transition_ms=20)})
    await _settle(0.03)
    beat = LightChange(on=True, hue=20, saturation=100, brightness=95, transition_ms=60)
    home.live.frame({entry["id"]: beat}, beat=True)
    await _settle(0.25)

    assert fades == [20, 60]


async def test_a_slow_bulb_fades_for_as_long_as_it_takes_between_frames() -> None:
    """A short fade on a bulb that took longer between changes jumped, held, and jumped: a flicker."""
    lamp = FakeBulb("10.0.0.36", "Desk lamp")
    home = _home(FakeNetwork(lamp))
    [entry] = await home.devices()
    fades: list[int | None] = []
    apply = home.apply

    async def slow(device_id: str, change: LightChange, *, confirm: bool = True) -> dict:
        fades.append(change.transition_ms)
        await asyncio.sleep(0.08)
        return await apply(device_id, change, confirm=confirm)

    home.apply = slow  # type: ignore[method-assign]
    await home.live.start([entry["id"]])
    for hue in (10, 20, 30):
        home.live.frame({entry["id"]: LightChange(on=True, hue=hue, saturation=100, brightness=60, transition_ms=20)})
        await _settle(0.03)
    await _settle(0.2)

    assert fades[0] == 20  # nothing to go by yet: the fade asked for
    assert len(fades) == 2 and 70 <= (fades[1] or 0) <= home_module._LONGEST_FADE_MS


async def test_stopping_puts_every_light_back_as_it_was() -> None:
    lit, dark = FakeBulb("10.0.0.36", "Desk lamp"), FakeBulb("10.0.0.47", "Bed lamp")
    lit.is_on, lit.light.brightness, lit.light.color_temp = True, 40, 2700
    home = _home(FakeNetwork(lit, dark))
    ids = [d["id"] for d in await home.devices()]

    await home.live.start(ids)
    home.live.frame({i: LightChange(on=True, hue=200, saturation=100, brightness=90) for i in ids})
    await _settle()
    await home.live.stop()

    assert (lit.is_on, lit.light.brightness, lit.light.color_temp) == (True, 40, 2700)
    assert dark.is_on is False and home.live.following == []


async def test_lights_are_put_back_by_themselves_when_frames_stop_coming() -> None:
    """The tab was closed, or the computer slept: nobody pressed Stop."""
    lamp = FakeBulb("10.0.0.36", "Desk lamp")
    home = _home(FakeNetwork(lamp), idle_seconds=0.1)
    [entry] = await home.devices()

    await home.live.start([entry["id"]])
    home.live.frame({entry["id"]: LightChange(on=True, hue=200, saturation=100, brightness=90)})
    await _settle(0.4)

    assert home.live.following == [] and lamp.is_on is False


async def test_only_the_lights_you_chose_follow_the_music() -> None:
    mine, friends = FakeBulb("10.0.0.36", "Desk lamp"), FakeBulb("10.0.0.47", "Sofa light")
    home = _home(FakeNetwork(mine, friends))
    found = {d["name"]: d["id"] for d in await home.devices()}

    with pytest.raises(LookupError):  # nothing was started
        home.live.frame({found["Desk lamp"]: LightChange(on=True, hue=1, saturation=1, brightness=1)})
    await home.live.start([found["Desk lamp"]])
    home.live.frame({i: LightChange(on=True, hue=120, saturation=100, brightness=80) for i in found.values()})
    await _settle()

    assert mine.light.sent and friends.light.sent == []


# ── A brand north wrote a tool for ──────────────────────────────────────────


async def test_a_brand_north_wrote_shows_beside_kasa_with_the_same_controls() -> None:
    home = _home(FakeNetwork(FakeBulb("10.0.0.36", "Desk lamp")), _Hue())

    found = {device["name"]: device for device in await home.devices()}

    assert found["Desk lamp"]["source"] == "kasa" and "/" not in found["Desk lamp"]["id"]  # yours stay yours
    lamp = found["Hall lamp"]
    assert lamp["id"] == "hue_lights/3" and lamp["source"] == "hue_lights" and lamp["kind"] == "light"
    assert lamp["can"] == {"dim": True, "color": True, "white": [2000, 6500]}
    assert (lamp["on"], lamp["brightness"], lamp["kelvin"]) == (False, 50, 2700)
    assert found["Kettle plug"]["kind"] == "switch" and found["Kettle plug"]["can"]["dim"] is False


async def test_a_change_on_the_page_reaches_the_brand_and_shows_what_it_reports() -> None:
    hue = _Hue()
    home = _home(None, hue)

    state = await home.apply("hue_lights/3", LightChange(on=True, brightness=80))

    assert hue.changes == [("3", LightChange(on=True, brightness=80))]
    assert (state["id"], state["on"], state["brightness"]) == ("hue_lights/3", True, 80)


async def test_a_brand_that_does_not_answer_is_named_and_the_rest_still_show() -> None:
    hue = _Hue()
    hue.answering = False
    home = _home(FakeNetwork(FakeBulb("10.0.0.36", "Desk lamp")), hue)

    found = await home.devices()

    assert [device["name"] for device in found] == ["Desk lamp"]
    assert "hue_lights: ConnectionError: the bridge at 192.168.1.20 did not answer" in home.problem


async def test_music_sync_follows_a_light_of_any_brand_and_puts_it_back() -> None:
    hue = _Hue()
    home = _home(None, hue)

    await home.live.start(["hue_lights/3"])
    home.live.frame({"hue_lights/3": LightChange(on=True, hue=200, saturation=100, brightness=90)})
    await _settle()
    await home.live.stop()

    assert ("3", LightChange(on=True, brightness=90, hue=200, saturation=100)) in hue.changes
    assert hue.devices["3"].on is False  # it was off before the music


async def test_a_brand_north_wrote_is_found_inside_the_wrapper_every_tool_it_writes_has() -> None:
    home = _home(None, LearnedTool(_Hue()))

    assert {device["id"] for device in await home.devices()} == {"hue_lights/3", "hue_lights/7"}


async def test_on_the_page_a_brand_gets_the_secret_it_declared() -> None:
    class Keyed(_Hue):
        uses_secrets = ("hue_bridge",)

        async def find(self) -> list[Device]:
            assert secret("hue_bridge") == "bridge-user-1234"
            return await super().find()

    secret_store.store().save("hue_bridge", "bridge-user-1234")

    found = await _home(None, Keyed()).devices()

    assert len(found) == 2


async def test_from_chat_a_brand_lists_and_changes_its_devices() -> None:
    hue = _Hue()
    hue.approvals = approvals(ApprovalMode.YOLO)  # from chat, a change goes to the approval layer

    listed = await hue.execute(ToolInput(params={"action": "list"}))
    dimmed = await hue.execute(ToolInput(params={"action": "set", "ids": ["hue_lights/3"], "brightness": 30}))
    cozy = await hue.execute(ToolInput(params={"action": "set", "ids": ["3"], "scene": "cozy"}))
    nothing = await hue.execute(ToolInput(params={"action": "set", "ids": ["3"]}))
    missing = await hue.execute(ToolInput(params={"action": "set", "ids": ["9"], "on": True}))

    assert [d["id"] for d in listed.data["devices"]] == ["hue_lights/3", "hue_lights/7"]
    assert "Hall lamp (hue_lights/3): off, 50%, white 2700K" in hue.format_output(listed.data)
    assert dimmed.success and hue.changes[0] == ("3", LightChange(brightness=30))
    assert cozy.success and hue.changes[1] == ("3", SCENES["cozy"])
    assert not nothing.success and "nothing to change" in nothing.error
    assert not missing.success and "9: KeyError" in missing.error
    assert hue.mutates({"action": "set"}) and not hue.mutates({"action": "list"})


async def test_a_device_of_a_brand_that_is_gone_says_to_add_it_again() -> None:
    with pytest.raises(LookupError, match="add the device again"):
        await _home(None, _Hue()).apply("govee_strips/1", LightChange(on=True))


# ── The guide north follows when you add a device ──────────────────────────


def _hue_example(tmp_path):
    from tests.unit.tools.test_tool_drafts import _block, _guide
    from tools.universal.create_tool import _load_tool_file, _validate_tool_contract

    code = _block(_guide("adding-a-home-device"), "python", "class HueLightsTool")
    path = tmp_path / "hue_lights.py"
    path.write_text(code, encoding="utf-8")
    tool = _load_tool_file(path, expected_name="hue_lights")
    _validate_tool_contract(tool)
    return code, tool


def test_the_home_device_guides_example_passes_every_check(tmp_path) -> None:
    from tools.universal._draft_checks import draft_problems

    code, tool = _hue_example(tmp_path)

    assert draft_problems(code, saved_secrets={"hue_bridge"}) == []
    assert isinstance(tool, DeviceTool) and tool.uses_secrets == ("hue_bridge",)


async def test_the_guides_example_shows_a_hue_lamp_in_the_pages_units_and_changes_it(tmp_path, monkeypatch) -> None:
    """The example is what the model copies: its unit conversions must be right against a real-shaped bridge."""
    import json

    import httpx

    _, tool = _hue_example(tmp_path)
    secret_store.store().save("hue_bridge", "bridge-user-1234")
    lamp = {
        "name": "Hall lamp",
        "state": {"on": False, "bri": 127, "hue": 8418, "sat": 140, "ct": 370, "colormode": "ct"},
    }
    sent: list[dict] = []

    def bridge(request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith("/api/bridge-user-1234/lights")
        if request.method == "PUT":
            state = json.loads(request.content)
            sent.append(state)
            lamp["state"] |= state | {"colormode": "ct" if "ct" in state else "hs"}
            return httpx.Response(200, json=[{"success": state}])
        return httpx.Response(200, json={"3": lamp} if request.url.path.endswith("/lights") else lamp)

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(bridge), **kw))
    home = _home(None, tool)

    [found] = await home.devices()
    changed = await home.apply("hue_lights/3", LightChange(on=True, hue=200, saturation=100))

    assert (found["id"], found["brightness"], found["kelvin"], found["can"]["white"]) == (
        "hue_lights/3",
        50,
        2703,
        [2000, 6500],
    )
    assert sent == [{"on": True, "hue": 36408, "sat": 254}]
    assert (changed["on"], changed["hue"], changed["saturation"]) == (True, 200, 100)


_PLUGS = "class Plugs(DeviceTool):\n    name = 'plugs'\n    async def find(self):\n        return []\n"


@pytest.mark.parametrize(
    ("draft", "fix"),
    [
        (_PLUGS, "has no change"),
        (
            _PLUGS + "    async def change(self, device_id, change):\n        return None\n"
            "    async def run(self, input):\n        return None\n",
            "defines run",
        ),
    ],
)
def test_a_device_tool_draft_that_misses_its_shape_comes_back_with_the_fix(draft, fix) -> None:
    from tools.universal._draft_checks import draft_problems

    assert any(fix in problem for problem in draft_problems(draft)), draft_problems(draft)


def test_a_device_tool_that_changes_devices_need_not_say_so_itself() -> None:
    """DeviceTool already says set changes things and list does not."""
    from tools.universal._draft_checks import draft_problems

    draft = (
        "import httpx\nclass Plugs(DeviceTool):\n    name = 'plugs'\n    async def find(self):\n        return []\n"
        "    async def change(self, device_id, change):\n"
        "        async with httpx.AsyncClient(timeout=5) as client:\n"
        "            await client.post('http://10.0.0.5/relay', json={'on': change.on})\n"
    )

    assert draft_problems(draft) == []


async def test_kasa_finding_nothing_is_not_news_beside_another_brands_lights() -> None:
    """With only Hue lamps in the house, "kasa discover found nothing" stood above them every time."""
    found_nothing = "kasa discover found nothing on this network"

    beside = _home(FakeNetwork(diagnostic=found_nothing), _Hue())
    await beside.devices()
    alone = _home(FakeNetwork(diagnostic=found_nothing))
    await alone.devices()

    assert beside.problem == ""
    assert alone.problem == found_nothing  # with nothing found at all, why is still said
