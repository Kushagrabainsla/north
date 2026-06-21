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
    monkeypatch.setattr(kasa_module, "_run_kasa_discover", list)
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
    monkeypatch.setattr(kasa_module, "_run_kasa_discover", list)
    result = await KasaTool().run(ToolInput(params={"action": "list"}))
    assert result.success is True
    assert result.data["devices"] == []
