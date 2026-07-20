"""Tests for NorthSettings live power/autonomy dials (persisted, env default, runtime set)."""

from __future__ import annotations

from pathlib import Path

from approval.mode import ApprovalMode
from config.strategy import NorthSettings, StrategyMode


def test_defaults_to_interactive(tmp_path: Path):
    ns = NorthSettings(tmp_path / "settings.json")
    assert ns.autonomy is ApprovalMode.INTERACTIVE


def test_uses_startup_default_when_no_file(tmp_path: Path):
    ns = NorthSettings(tmp_path / "settings.json", default_approval_mode=ApprovalMode.AUTO)
    assert ns.autonomy is ApprovalMode.AUTO


def test_set_autonomy_persists(tmp_path: Path):
    path = tmp_path / "settings.json"
    NorthSettings(path).set_autonomy(ApprovalMode.AUTONOMOUS)
    # a fresh instance reads the persisted value, overriding the startup default
    reloaded = NorthSettings(path, default_approval_mode=ApprovalMode.INTERACTIVE)
    assert reloaded.autonomy is ApprovalMode.AUTONOMOUS


def test_file_value_overrides_startup_default(tmp_path: Path):
    path = tmp_path / "settings.json"
    NorthSettings(path).set_autonomy(ApprovalMode.AUTO)
    ns = NorthSettings(path, default_approval_mode=ApprovalMode.AUTONOMOUS)
    assert ns.autonomy is ApprovalMode.AUTO


def test_set_mode_is_live_on_same_instance(tmp_path: Path):
    ns = NorthSettings(tmp_path / "settings.json")
    assert ns.autonomy is ApprovalMode.INTERACTIVE
    ns.set_autonomy(ApprovalMode.AUTONOMOUS)
    assert ns.autonomy is ApprovalMode.AUTONOMOUS  # no reload needed


def test_power_and_autonomy_coexist(tmp_path: Path):
    path = tmp_path / "settings.json"
    ns = NorthSettings(path)
    ns.set_power(StrategyMode.SPORT)
    ns.set_autonomy(ApprovalMode.AUTO)
    reloaded = NorthSettings(path)
    assert reloaded.power is StrategyMode.SPORT
    assert reloaded.autonomy is ApprovalMode.AUTO
