"""Tests for NorthSettings live power/autonomy dials (persisted, env default, runtime set)."""

from __future__ import annotations

from pathlib import Path

import pytest

from config.approval_mode import ApprovalMode
from config.strategy import NorthSettings, StrategyMode


def test_defaults_to_interactive(tmp_path: Path):
    ns = NorthSettings(tmp_path / "settings.json")
    assert ns.autonomy is ApprovalMode.ASK


def test_uses_startup_default_when_no_file(tmp_path: Path):
    ns = NorthSettings(tmp_path / "settings.json", default_approval_mode=ApprovalMode.SAFE)
    assert ns.autonomy is ApprovalMode.SAFE


def test_set_autonomy_persists(tmp_path: Path):
    path = tmp_path / "settings.json"
    NorthSettings(path).set_autonomy(ApprovalMode.AUTONOMOUS)
    # a fresh instance reads the persisted value, overriding the startup default
    reloaded = NorthSettings(path, default_approval_mode=ApprovalMode.ASK)
    assert reloaded.autonomy is ApprovalMode.AUTONOMOUS


def test_file_value_overrides_startup_default(tmp_path: Path):
    path = tmp_path / "settings.json"
    NorthSettings(path).set_autonomy(ApprovalMode.SAFE)
    ns = NorthSettings(path, default_approval_mode=ApprovalMode.AUTONOMOUS)
    assert ns.autonomy is ApprovalMode.SAFE


def test_set_mode_is_live_on_same_instance(tmp_path: Path):
    ns = NorthSettings(tmp_path / "settings.json")
    assert ns.autonomy is ApprovalMode.ASK
    ns.set_autonomy(ApprovalMode.AUTONOMOUS)
    assert ns.autonomy is ApprovalMode.AUTONOMOUS  # no reload needed


def test_power_and_autonomy_coexist(tmp_path: Path):
    path = tmp_path / "settings.json"
    ns = NorthSettings(path)
    ns.set_power(StrategyMode.SPORT)
    ns.set_autonomy(ApprovalMode.SAFE)
    reloaded = NorthSettings(path)
    assert reloaded.power is StrategyMode.SPORT
    assert reloaded.autonomy is ApprovalMode.SAFE


def test_timezone_is_named_and_persisted(tmp_path: Path):
    path = tmp_path / "settings.json"
    settings = NorthSettings(path)

    settings.set_timezone("America/Los_Angeles")

    assert settings.timezone == "America/Los_Angeles"
    assert NorthSettings(path).timezone == "America/Los_Angeles"


def test_invalid_timezone_is_refused(tmp_path: Path):
    settings = NorthSettings(tmp_path / "settings.json")

    with pytest.raises(ValueError, match="Unknown timezone"):
        settings.set_timezone("Mars/Olympus_Mons")
