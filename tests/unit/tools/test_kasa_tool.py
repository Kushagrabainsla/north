from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools.specialized import kasa_tool


def test_discovery_detaches_stdin_and_parses_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=0, stdout="Host: 192.168.1.20\n== Desk lamp - bulb ==", stderr="")

    monkeypatch.setattr(kasa_tool.shutil, "which", lambda _: "/usr/bin/kasa")
    monkeypatch.setattr(kasa_tool.subprocess, "run", fake_run)

    pairs, diagnostic = kasa_tool._run_kasa_discover()

    assert pairs == [("192.168.1.20", "Desk lamp")]
    assert diagnostic == ""
    assert calls[0]["stdin"] is kasa_tool.subprocess.DEVNULL


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
    assert diagnostic == ""
    assert commands == [["/usr/bin/kasa", "discover"], [kasa_tool.sys.executable, "-m", "kasa", "discover"]]


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
