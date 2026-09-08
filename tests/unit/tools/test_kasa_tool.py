from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools.specialized import kasa_tool

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

    monkeypatch.setattr(kasa_tool.shutil, "which", lambda _: "/usr/bin/kasa")
    monkeypatch.setattr(kasa_tool.subprocess, "run", fake_run)

    pairs, diagnostic = kasa_tool._run_kasa_discover()

    # A device that has not authenticated reports no alias; the model stands in.
    assert pairs == [("10.0.0.36", "Desk lamp"), ("10.0.0.47", "KL125(US)")]
    assert diagnostic == ""
    assert calls[0]["stdin"] is kasa_tool.subprocess.DEVNULL
    assert "--json" in calls[0]["cmd"]


def test_discovery_uses_module_fallback_after_binary_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        if len(commands) == 1:
            return SimpleNamespace(returncode=1, stdout="", stderr="binary failed")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kasa_tool.shutil, "which", lambda _: "/usr/bin/kasa")
    monkeypatch.setattr(kasa_tool.subprocess, "run", fake_run)

    pairs, diagnostic = kasa_tool._run_kasa_discover()

    assert pairs == []
    # Never silent. Finding nothing and saying nothing is the failure this tool
    # spent months producing: two bulbs present, "no devices found" reported.
    assert diagnostic
    assert commands == [
        ["/usr/bin/kasa", "--json", "discover"],
        [kasa_tool.sys.executable, "-m", "kasa", "--json", "discover"],
    ]


def test_devices_that_refuse_the_login_say_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real fault on a modern network, and the one that reached the user as silence."""
    monkeypatch.setattr(kasa_tool.shutil, "which", lambda _: "/usr/bin/kasa")
    monkeypatch.setattr(
        kasa_tool.subprocess,
        "run",
        lambda cmd, **kw: SimpleNamespace(
            returncode=0, stdout="", stderr="== Authentication failed for device ==\n== Authentication failed ==\n"
        ),
    )

    pairs, diagnostic = kasa_tool._run_kasa_discover()

    assert pairs == []
    assert "2 device(s) refused authentication" in diagnostic
    assert "NORTH_KASA_USERNAME" in diagnostic


def test_the_login_is_passed_to_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    from config.settings import settings

    monkeypatch.setattr(settings, "kasa_username", "me@example.com", raising=False)
    monkeypatch.setattr(settings, "kasa_password", "hunter2", raising=False)
    assert kasa_tool._credential_args() == ["--username", "me@example.com", "--password", "hunter2"]


def test_no_login_configured_passes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from config.settings import settings

    monkeypatch.setattr(settings, "kasa_username", "", raising=False)
    monkeypatch.setattr(settings, "kasa_password", "", raising=False)
    assert kasa_tool._credential_args() == []


def test_the_password_never_reaches_an_error_message() -> None:
    """Diagnostics quote the command they ran, and one of its arguments is a secret."""
    redacted = kasa_tool._redacted(["kasa", "--username", "me@example.com", "--password", "hunter2", "discover"])
    assert "hunter2" not in redacted
    assert redacted[4] == "***"
    assert "me@example.com" in redacted  # only the password is a secret


def test_unparseable_output_is_not_a_device() -> None:
    assert kasa_tool._parse_discovery("not json at all") == []
    assert kasa_tool._parse_discovery("[]") == []


def test_action_aliases_cover_natural_tool_calls() -> None:
    assert kasa_tool._ACTION_ALIASES["set_brightness"] == "brightness"
    assert kasa_tool._ACTION_ALIASES["turn_on"] == "on"
    assert kasa_tool._ACTION_ALIASES["apply_scene"] == "scene"


@pytest.mark.asyncio
async def test_discovery_failure_is_reported_as_tool_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_to_thread(_func):
        return [], "kasa discover exited 1: permission denied"

    monkeypatch.setattr(kasa_tool.asyncio, "to_thread", fake_to_thread)

    _, _, early = await kasa_tool.KasaTool._discover_and_connect()

    assert early is not None
    assert early.success is False
    assert "permission denied" in (early.error or "")


@pytest.mark.asyncio
async def test_discovered_but_unreachable_devices_are_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_to_thread(_func):
        return [("192.168.1.20", "Desk lamp")], ""

    async def fake_connect(_pairs):
        return {}, ["Desk lamp (192.168.1.20): TimeoutError: timed out"]

    monkeypatch.setattr(kasa_tool.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(kasa_tool, "_connect_devices", fake_connect)

    _, _, early = await kasa_tool.KasaTool._discover_and_connect()

    assert early is not None
    assert early.success is False
    assert "none could be reached" in (early.error or "")
    assert "TimeoutError" in (early.error or "")


@pytest.mark.asyncio
async def test_moody_scene_applies_brightness_and_warmth() -> None:
    class FakeDevice:
        alias = "Desk lamp"
        is_on = True
        brightness = 100
        color_temp = 4000
        hsv = None

        async def set_brightness(self, value):
            self.brightness = value

        async def set_color_temp(self, value):
            self.color_temp = value

        async def update(self):
            return None

    results, errors = await kasa_tool._apply_scene_to_devices(
        {"10.0.0.1": FakeDevice()}, "moody", {"10.0.0.1": "Desk lamp"}
    )

    assert errors == []
    assert results[0]["brightness"] == 30
    assert results[0]["color_temp"] == 2700


@pytest.mark.asyncio
async def test_action_retries_transient_failure_and_verifies_state() -> None:
    class FlakyDevice:
        alias = "Desk lamp"
        is_on = False
        brightness = 10
        color_temp = 4000
        hsv = None
        attempts = 0

        async def set_brightness(self, value):
            self.attempts += 1
            if self.attempts == 1:
                raise TimeoutError("temporary timeout")
            self.brightness = value

        async def update(self):
            return None

    dev = FlakyDevice()
    results, errors = await kasa_tool._apply_action_to_devices(
        {"10.0.0.1": dev}, "brightness", kasa_tool._ActionParams(brightness=40), {"10.0.0.1": "Desk lamp"}
    )

    assert errors == []
    assert results[0]["brightness"] == 40
    assert dev.attempts == 2


@pytest.mark.asyncio
async def test_scene_reports_unsupported_device() -> None:
    class Plug:
        alias = "Coffee plug"
        is_on = True
        is_dimmable = False
        is_color = False
        is_variable_color_temp = False

        async def update(self):
            return None

    results, errors = await kasa_tool._apply_scene_to_devices(
        {"10.0.0.2": Plug()}, "party", {"10.0.0.2": "Coffee plug"}
    )

    assert results == []
    assert "does not support" in errors[0]


@pytest.mark.asyncio
async def test_list_does_not_need_a_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """The action the "device is required" error tells you to run must not need one.

    It did. `list` was not exempt from the target check, so the only route out of
    that error was the error itself.
    """
    from tools.models import ToolInput

    async def fake_discover(*_args, **_kwargs):
        return {}, {}, None

    monkeypatch.setattr(kasa_tool.KasaTool, "_discover_and_connect", staticmethod(fake_discover))
    out = await kasa_tool.KasaTool().run(ToolInput(params={"action": "list"}))

    assert "device' is required" not in (out.error or "")


@pytest.mark.asyncio
async def test_a_control_action_still_needs_a_device() -> None:
    """The guard exists so a command can never fan out across the whole house."""
    from tools.models import ToolInput

    out = await kasa_tool.KasaTool().run(ToolInput(params={"action": "off"}))
    assert out.success is False
    assert "device' is required" in (out.error or "")


@pytest.mark.asyncio
async def test_devices_are_released_after_a_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each open device holds an HTTP session; nothing used to close them."""
    from tools.models import ToolInput

    closed: list[str] = []

    class _Bulb:
        alias = "Desk lamp"
        is_on = True

        async def disconnect(self) -> None:
            closed.append(self.alias)

    async def fake_discover(*_args, **_kwargs):
        return {"10.0.0.36": _Bulb()}, {"10.0.0.36": "Desk lamp"}, None

    monkeypatch.setattr(kasa_tool.KasaTool, "_discover_and_connect", staticmethod(fake_discover))
    await kasa_tool.KasaTool().run(ToolInput(params={"action": "list"}))

    assert closed == ["Desk lamp"]


@pytest.mark.asyncio
async def test_dim_is_the_word_people_use(monkeypatch: pytest.MonkeyPatch) -> None:
    """"dim my lights to 50%" reached the tool as an undefined action and was
    reported as a missing device, which blamed the request for the vocabulary."""
    from tools.models import ToolInput

    seen: list[str] = []

    async def fake_discover(*_args, **_kwargs):
        seen.append("discovered")
        return {}, {}, kasa_tool.ToolOutput(success=False, error="stop here")

    monkeypatch.setattr(kasa_tool.KasaTool, "_discover_and_connect", staticmethod(fake_discover))
    out = await kasa_tool.KasaTool().run(ToolInput(params={"action": "dim", "brightness": 50}))

    # It got as far as looking for devices, rather than being turned away first.
    assert seen == ["discovered"]
    assert "device' is required" not in (out.error or "")


@pytest.mark.asyncio
async def test_an_unknown_action_says_it_is_unknown() -> None:
    """It used to answer "device is required" for every word nobody had defined."""
    from tools.models import ToolInput

    out = await kasa_tool.KasaTool().run(ToolInput(params={"action": "banana"}))

    assert out.success is False
    assert "Unknown action" in (out.error or "")
    assert "device" not in (out.error or "").split("Valid actions")[0]


def test_every_alias_lands_on_a_real_action() -> None:
    """The alias table and the action table cannot drift apart unnoticed."""
    assert set(kasa_tool._ACTION_ALIASES.values()) <= kasa_tool._KNOWN_ACTIONS
