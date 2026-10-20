"""The screen_brightness tool reads and sets every screen of this Mac, asking first only to change one."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from approval.policy import ActionKind
from approval.unattended_rules import _BUILTIN_DEVICE
from tools.models import ToolInput
from tools.specialized import _mac_display, screen_brightness
from tools.specialized._display import Screen
from tools.specialized.screen_brightness import ScreenBrightnessTool

_LAPTOP = Screen("Built-in Retina Display", built_in=True)
_ALIENWARE = Screen("AW3425DWM", built_in=False)
_DELL = Screen("DELL P2419H", built_in=False)
_NO_DDC = OSError("the monitor is not answering brightness commands")


def _display(monkeypatch: pytest.MonkeyPatch, levels: dict[Screen, int | OSError]) -> list:
    calls: list[tuple[str, int | None]] = []

    class FakeDisplay:
        @staticmethod
        def available() -> bool:
            return True

        @staticmethod
        async def screens() -> list[Screen]:
            return list(levels)

        @staticmethod
        async def brightness(screen: Screen, level: int | None = None) -> int:
            calls.append((screen.name, level))
            reading = levels[screen]
            if isinstance(reading, OSError):
                raise reading
            return reading if level is None else level

    monkeypatch.setattr(screen_brightness, "current", lambda: FakeDisplay)
    return calls


async def _run(**params) -> SimpleNamespace:
    tool = ScreenBrightnessTool()
    output = await tool.run(ToolInput(params=params))
    text = tool.format_output(output.data) if output.success else output.error
    return SimpleNamespace(success=output.success, text=text)


@pytest.mark.asyncio
async def test_a_read_lists_every_screen_and_never_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    _display(monkeypatch, {_LAPTOP: 24, _ALIENWARE: 100})

    result = await _run()

    assert result.text == "- Built-in Retina Display: at 24%\n- AW3425DWM: at 100%"
    assert not ScreenBrightnessTool().mutates({"workspace": "/Users/me"})


@pytest.mark.parametrize(
    ("params", "sets"),
    [
        ({"action": "get", "brightness": 0}, False),  # an agent's way to read; it set the screen to 0%
        ({"action": "read"}, False),  # an unknown action reads, it never writes
        ({"action": "set", "brightness": 40}, True),
        ({"brightness": 40}, True),  # the planner's way to set: a level and nothing else
    ],
)
@pytest.mark.asyncio
async def test_only_a_set_or_a_bare_level_changes_anything(
    monkeypatch: pytest.MonkeyPatch, params: dict, sets: bool
) -> None:
    calls = _display(monkeypatch, {_LAPTOP: 24})

    await _run(**params)

    assert calls == [("Built-in Retina Display", 40 if sets else None)]
    assert ScreenBrightnessTool().mutates(params) is sets


@pytest.mark.asyncio
async def test_a_set_without_a_level_touches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _display(monkeypatch, {_LAPTOP: 24})

    result = await _run(action="set")

    assert not result.success and "0 to 100" in result.text
    assert calls == []


@pytest.mark.asyncio
async def test_a_set_without_a_screen_sets_all_and_reports_what_each_reads_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _display(monkeypatch, {_LAPTOP: 24, _ALIENWARE: 100})

    result = await _run(brightness=40)

    assert result.text == "- Built-in Retina Display: set to 40%\n- AW3425DWM: set to 40%"
    assert calls == [("Built-in Retina Display", 40), ("AW3425DWM", 40)]


@pytest.mark.parametrize(
    ("screen", "chosen"),
    [
        ("aw3425dwm", ["AW3425DWM"]),
        ("dell", ["DELL P2419H"]),
        ("Built-in", ["Built-in Retina Display"]),
        ("p2419", ["DELL P2419H"]),
    ],
)
@pytest.mark.asyncio
async def test_screens_are_picked_by_their_name_or_a_part_of_it(
    monkeypatch: pytest.MonkeyPatch, screen: str, chosen: list[str]
) -> None:
    calls = _display(monkeypatch, {_LAPTOP: 24, _ALIENWARE: 100, _DELL: 50})

    await _run(brightness=30, screen=screen)

    assert calls == [(name, 30) for name in chosen]


@pytest.mark.parametrize("screen", ["Alienware", "laptop"])
@pytest.mark.asyncio
async def test_a_word_no_name_contains_lists_the_names_to_ask_with(
    monkeypatch: pytest.MonkeyPatch, screen: str
) -> None:
    """No brand table or list of words for the laptop: the agent reads the names and asks again."""
    calls = _display(monkeypatch, {_LAPTOP: 24, _ALIENWARE: 100})

    result = await _run(brightness=30, screen=screen)

    assert result.text == f"No screen matches '{screen}'. Screens: Built-in Retina Display, AW3425DWM."
    assert calls == []


@pytest.mark.asyncio
async def test_a_monitor_that_does_not_answer_is_named_and_the_others_still_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _display(monkeypatch, {_LAPTOP: 24, _DELL: _NO_DDC})

    result = await _run(brightness=30)

    assert result.success
    assert result.text.splitlines() == [
        "- Built-in Retina Display: set to 30%",
        "- DELL P2419H: not changed - the monitor is not answering brightness commands",
    ]


@pytest.mark.asyncio
async def test_when_no_screen_changes_the_call_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _display(monkeypatch, {_DELL: _NO_DDC})

    result = await _run(brightness=30)

    assert not result.success and "- DELL P2419H: not changed" in result.text


@pytest.mark.asyncio
async def test_an_unknown_screen_lists_the_ones_there_are(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _display(monkeypatch, {_LAPTOP: 24, _ALIENWARE: 100})

    result = await _run(brightness=30, screen="projector")

    assert result.text == "No screen matches 'projector'. Screens: Built-in Retina Display, AW3425DWM."
    assert calls == []


@pytest.mark.asyncio
async def test_a_change_is_described_as_a_reversible_device_action() -> None:
    """set_brightness is on the safe list, so an unattended run may dim a screen without a card."""
    tool = ScreenBrightnessTool()

    request = await tool.describe(ToolInput(params={"brightness": 30, "screen": "AW3425DWM"}))

    assert tool.mutates({"brightness": 30})
    assert request is not None and request.action.kind is ActionKind.DEVICE
    assert request.action.operation == "set_brightness" and "set_brightness" in _BUILTIN_DEVICE
    assert request.message == "Set the brightness of **AW3425DWM** to **30%**"


@pytest.mark.parametrize("params", [{"brightness": None}, {"brightness": 140}, {"brightness": "x"}])
@pytest.mark.asyncio
async def test_a_set_without_a_valid_level_touches_nothing(monkeypatch: pytest.MonkeyPatch, params: dict) -> None:
    calls = _display(monkeypatch, {_LAPTOP: 24})

    result = await _run(**params)

    assert not result.success and "0 to 100" in result.text
    assert calls == []
    assert await ScreenBrightnessTool().describe(ToolInput(params=params)) is None


@pytest.mark.asyncio
async def test_a_computer_without_support_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(screen_brightness, "current", lambda: None)

    result = await _run()

    assert not result.success and "not supported" in result.text


def _commands(monkeypatch: pytest.MonkeyPatch, answers: dict[tuple[str, ...], str]) -> list[tuple[str, ...]]:
    """Stand in for osascript and m1ddc: each argv (osascript's script left out) gets its answer."""
    ran: list[tuple[str, ...]] = []

    async def fake_run(argv: list[str]) -> str:
        key = tuple(part for part in argv if part != _mac_display._BUILT_IN_SCRIPT)
        ran.append(key)
        if key not in answers:
            raise OSError(f"unexpected command {key}")
        return answers[key]

    monkeypatch.setattr(_mac_display, "_run", fake_run)
    monkeypatch.setattr(_mac_display.shutil, "which", lambda name: f"/opt/homebrew/bin/{name}")
    return ran


_AW = "1C056EE6-3EC6-4232-8AA5-D2794825F034"
_LISTING = (
    "[1] (null) (37D8832A-2D66-02CA-B9F7-8F30A301B230)\n"
    f"[2] AW3425DWM ({_AW})\n"
    "[3] DELL P2419H (9FC0EAFB-6989-44A0-899C-AE9EC522C0E6)\n"
    "[4] DELL P2419H (11111111-2222-3333-4444-555555555555)"
)


@pytest.mark.asyncio
async def test_the_mac_lists_its_own_screen_first_then_each_monitor(monkeypatch: pytest.MonkeyPatch) -> None:
    _commands(
        monkeypatch,
        {
            ("osascript", "-l", "JavaScript", "-e"): "Built-in Retina Display\t0.2412",
            ("m1ddc", "display", "list"): _LISTING,
        },
    )

    screens = await _mac_display.MacDisplay.screens()

    assert [screen.name for screen in screens] == [
        "Built-in Retina Display",
        "AW3425DWM",
        "DELL P2419H",
        "DELL P2419H (2)",
    ]
    assert [screen.built_in for screen in screens] == [True, False, False, False]


@pytest.mark.asyncio
async def test_the_built_in_screen_gets_a_fraction_and_answers_in_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _commands(monkeypatch, {("osascript", "-l", "JavaScript", "-e", "0.3000"): "Built-in Retina Display\t0.3"})

    assert await _mac_display.MacDisplay.brightness(_LAPTOP, 30) == 30
    assert ran == [("osascript", "-l", "JavaScript", "-e", "0.3000")]


@pytest.mark.asyncio
async def test_a_monitor_is_set_over_ddc_and_read_back(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _commands(
        monkeypatch,
        {
            ("m1ddc", "display", "list"): _LISTING,
            ("m1ddc", "display", _AW, "max", "luminance"): "100",
            ("m1ddc", "display", _AW, "set", "luminance", "40"): "40",
            ("m1ddc", "display", _AW, "get", "luminance"): "40",
        },
    )

    assert await _mac_display.MacDisplay.brightness(_ALIENWARE, 40) == 40
    assert ("m1ddc", "display", _AW, "set", "luminance", "40") in ran


@pytest.mark.asyncio
async def test_a_monitor_answering_noise_is_never_written_to(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real Dell behind an adapter: 16% brightness, a maximum of -126, volume 98 with no speakers."""
    dell = "9FC0EAFB-6989-44A0-899C-AE9EC522C0E6"
    ran = _commands(
        monkeypatch,
        {("m1ddc", "display", "list"): _LISTING, ("m1ddc", "display", dell, "max", "luminance"): "-126"},
    )

    with pytest.raises(OSError, match="DDC/CI"):
        await _mac_display.MacDisplay.brightness(_DELL, 30)
    assert not any("set" in command for command in ran)


@pytest.mark.asyncio
async def test_a_monitor_that_ignores_the_change_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    _commands(
        monkeypatch,
        {
            ("m1ddc", "display", "list"): _LISTING,
            ("m1ddc", "display", _AW, "max", "luminance"): "100",
            ("m1ddc", "display", _AW, "set", "luminance", "17"): "17",
            ("m1ddc", "display", _AW, "get", "luminance"): "16",
        },
    )

    with pytest.raises(OSError, match="not answering brightness commands"):
        await _mac_display.MacDisplay.brightness(_ALIENWARE, 17)


def test_an_osascript_error_becomes_its_plain_reason() -> None:
    assert (
        _mac_display._reason("execution error: Error: Error: the built-in screen is off (-2700)\n")
        == "the built-in screen is off"
    )
