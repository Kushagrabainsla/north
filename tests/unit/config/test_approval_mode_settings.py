"""Tests for NorthSettings live approval_mode (persisted, env default, runtime set)."""

from __future__ import annotations

from pathlib import Path

from approval.mode import ApprovalMode
from config.strategy import NorthSettings


def test_defaults_to_interactive(tmp_path: Path):
    ns = NorthSettings(tmp_path / "settings.json")
    assert ns.approval_mode is ApprovalMode.INTERACTIVE


def test_uses_startup_default_when_no_file(tmp_path: Path):
    ns = NorthSettings(tmp_path / "settings.json", default_approval_mode=ApprovalMode.AUTO)
    assert ns.approval_mode is ApprovalMode.AUTO


def test_set_approval_mode_persists(tmp_path: Path):
    path = tmp_path / "settings.json"
    NorthSettings(path).set_approval_mode(ApprovalMode.AUTONOMOUS)
    # a fresh instance reads the persisted value, overriding the startup default
    reloaded = NorthSettings(path, default_approval_mode=ApprovalMode.INTERACTIVE)
    assert reloaded.approval_mode is ApprovalMode.AUTONOMOUS


def test_file_value_overrides_startup_default(tmp_path: Path):
    path = tmp_path / "settings.json"
    NorthSettings(path).set_approval_mode(ApprovalMode.AUTO)
    ns = NorthSettings(path, default_approval_mode=ApprovalMode.AUTONOMOUS)
    assert ns.approval_mode is ApprovalMode.AUTO


def test_set_mode_is_live_on_same_instance(tmp_path: Path):
    ns = NorthSettings(tmp_path / "settings.json")
    assert ns.approval_mode is ApprovalMode.INTERACTIVE
    ns.set_approval_mode(ApprovalMode.AUTONOMOUS)
    assert ns.approval_mode is ApprovalMode.AUTONOMOUS  # no reload needed


def test_strategy_and_mode_coexist(tmp_path: Path):
    from config.strategy import StrategyMode

    path = tmp_path / "settings.json"
    ns = NorthSettings(path)
    ns.set_strategy(StrategyMode.SPORT)
    ns.set_approval_mode(ApprovalMode.AUTO)
    reloaded = NorthSettings(path)
    assert reloaded.strategy is StrategyMode.SPORT
    assert reloaded.approval_mode is ApprovalMode.AUTO
