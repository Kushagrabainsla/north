"""Tests for NorthSettings preferred-models load/override/persist."""

from __future__ import annotations

import json

from config.strategy import NorthSettings, StrategyMode


def test_default_preferred_models_used_when_no_file(tmp_path):
    ns = NorthSettings(tmp_path / "settings.json", default_preferred_models={"reasoning": ["a", "b"]})
    assert ns.preferred_models == {"reasoning": ["a", "b"]}


def test_settings_json_overrides_default(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"preferred_models": {"reasoning": ["from-file"]}}), encoding="utf-8")
    ns = NorthSettings(path, default_preferred_models={"reasoning": ["default"]})
    assert ns.preferred_models == {"reasoning": ["from-file"]}


def test_default_preferred_models_not_frozen_on_routine_save(tmp_path):
    # A routine save (e.g. changing strategy) must NOT freeze the *default*
    # preferred_models into settings.json - otherwise future default improvements
    # could never reach an install that ever saved a setting.
    path = tmp_path / "settings.json"
    ns = NorthSettings(path, default_preferred_models={"reasoning": ["old-default"]})
    ns.set_strategy(StrategyMode.SPORT)

    data = json.loads(path.read_text())
    assert "preferred_models" not in data  # default was not persisted
    assert data["strategy"] == "sport"

    # A newer version with an improved default picks it up (not frozen).
    reloaded = NorthSettings(path, default_preferred_models={"reasoning": ["new-default"]})
    assert reloaded.preferred_models == {"reasoning": ["new-default"]}


def test_explicit_preferred_models_persist_and_override_default(tmp_path):
    path = tmp_path / "settings.json"
    ns = NorthSettings(path, default_preferred_models={"reasoning": ["d"]})
    ns.set_preferred_models({"reasoning": ["x", "y"]})  # deliberate choice
    data = json.loads(path.read_text())
    assert data["preferred_models"] == {"reasoning": ["x", "y"]}
    # Persisted explicit choice overrides even a different code default.
    reloaded = NorthSettings(path, default_preferred_models={"reasoning": ["d"]})
    assert reloaded.preferred_models == {"reasoning": ["x", "y"]}


def test_set_preferred_models_coerces_and_persists(tmp_path):
    path = tmp_path / "settings.json"
    ns = NorthSettings(path)
    ns.set_preferred_models({"reasoning": ["x", "y"], "bad": 5, "empty": []})
    reloaded = NorthSettings(path)
    assert reloaded.preferred_models == {"reasoning": ["x", "y"]}


def test_malformed_preferred_models_falls_back(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"preferred_models": "not-a-dict"}), encoding="utf-8")
    ns = NorthSettings(path, default_preferred_models={"reasoning": ["d"]})
    # An unusable value coerces to empty rather than raising.
    assert ns.preferred_models == {}
