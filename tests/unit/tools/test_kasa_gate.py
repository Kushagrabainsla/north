"""Tests for KasaTool gating and explicit targeting (review finding R2#13)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from tools.models import ToolInput
from tools.specialized import kasa_tool as kasa_module
from tools.specialized.kasa_tool import KasaTool


def test_kasa_is_marked_mutating() -> None:
    assert KasaTool.is_mutating is True


async def test_control_action_requires_explicit_device() -> None:
    result = await KasaTool().run(ToolInput(params={"action": "on"}))
    assert result.success is False
    assert "device" in result.error


async def test_control_action_fails_closed_without_gate() -> None:
    result = await KasaTool().run(ToolInput(params={"action": "off", "device": "lamp"}))
    assert result.success is False
    assert "fail closed" in result.error


async def test_control_action_refused_on_reject(monkeypatch) -> None:
    # Discovery returns no devices (empty pairs + empty diagnostic).
    def _fake_discover():
        return [], ""

    monkeypatch.setattr(kasa_module, "_run_kasa_discover", _fake_discover)
    store = MagicMock()
    resolved = MagicMock()
    resolved.chosen_option = "Reject"
    resolved.status = "rejected"
    store.wait_for_decision = AsyncMock(return_value=resolved)

    tool = KasaTool(approval_store=store)
    result = await tool.run(ToolInput(params={"action": "on", "device": "lamp"}))
    assert result.success is False
    assert "rejected" in result.error.lower()


async def test_list_works_without_device_or_gate(monkeypatch) -> None:
    def _fake_discover():
        return [], ""

    monkeypatch.setattr(kasa_module, "_run_kasa_discover", _fake_discover)
    result = await KasaTool().run(ToolInput(params={"action": "list"}))
    assert result.success is True
    assert result.data["devices"] == []


async def test_discovery_failure_is_diagnosed(monkeypatch) -> None:
    """A discovery that fails surfaces a diagnostic instead of a silent empty."""

    def _fake_discover():
        return [], "kasa discover produced output but no devices parsed; stderr: (none)"

    monkeypatch.setattr(kasa_module, "_run_kasa_discover", _fake_discover)
    result = await KasaTool().run(ToolInput(params={"action": "list"}))
    assert result.success is True
    assert result.data["devices"] == []
    assert "no devices parsed" in result.data["message"].lower()


async def test_discovery_success_returns_pairs(monkeypatch) -> None:
    """A successful discovery returns parsed pairs with aliases."""

    def _fake_discover():
        return [("10.0.0.36", "Desk lamp"), ("10.0.0.47", "Upper desk lamp")], ""

    monkeypatch.setattr(kasa_module, "_run_kasa_discover", _fake_discover)

    async def _fake_connect(pairs):
        return {ip: MagicMock(alias=alias, is_on=True) for ip, alias in pairs}

    monkeypatch.setattr(kasa_module, "_connect_devices", _fake_connect)
    result = await KasaTool().run(ToolInput(params={"action": "list"}))
    assert result.success is True
    aliases = {d["alias"] for d in result.data["devices"]}
    assert aliases == {"Desk lamp", "Upper desk lamp"}
