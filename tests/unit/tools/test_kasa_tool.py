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
        return {}

    monkeypatch.setattr(kasa_tool.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(kasa_tool, "_connect_devices", fake_connect)

    _, _, early = await kasa_tool.KasaTool._discover_and_connect()

    assert early is not None
    assert early.success is False
    assert "none could be reached" in (early.error or "")
