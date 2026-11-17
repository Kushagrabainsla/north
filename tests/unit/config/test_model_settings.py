"""The Models panel: a Default row and a row per kind of work, persisted across restarts (#80)."""

from __future__ import annotations

import json
from pathlib import Path

from config.strategy import ModelChoice, NorthSettings, StrategyMode, WorkKind


def test_defaults_to_auto_cruise_for_every_kind(tmp_path: Path) -> None:
    settings = NorthSettings(tmp_path / "settings.json")
    assert settings.default_model == ModelChoice()
    assert settings.default_model.auto
    assert all(settings.model_for(kind) == ModelChoice() for kind in WorkKind)
    assert all(settings.kind_model(kind) is None for kind in WorkKind)  # "Same as default"


def test_a_kind_uses_the_default_until_it_has_its_own_row(tmp_path: Path) -> None:
    settings = NorthSettings(tmp_path / "settings.json")
    settings.set_default_model(ModelChoice(model="anthropic:claude-sonnet-5-5"))
    settings.set_kind_model(WorkKind.BACKGROUND, ModelChoice(power=StrategyMode.ECO))

    assert settings.model_for(WorkKind.TASKS).model == "anthropic:claude-sonnet-5-5"
    assert settings.model_for(WorkKind.BACKGROUND) == ModelChoice(power=StrategyMode.ECO)

    settings.set_kind_model(WorkKind.BACKGROUND, None)
    assert settings.model_for(WorkKind.BACKGROUND).model == "anthropic:claude-sonnet-5-5"


def test_rows_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    settings = NorthSettings(path)
    settings.set_default_model(ModelChoice(model="groq:qwen3-32b"))
    settings.set_kind_model(WorkKind.CHECKS, ModelChoice(power=StrategyMode.SPORT))

    reloaded = NorthSettings(path)
    assert reloaded.default_model.model == "groq:qwen3-32b"
    assert reloaded.kind_model(WorkKind.CHECKS) == ModelChoice(power=StrategyMode.SPORT)
    assert reloaded.kind_model(WorkKind.TASKS) is None


def test_a_row_set_back_to_auto_has_its_power_again(tmp_path: Path) -> None:
    """Power is kept under a chosen model, so it is not lost by choosing one for a while."""
    path = tmp_path / "settings.json"
    settings = NorthSettings(path)
    settings.set_power(StrategyMode.ECO)
    settings.set_default_model(ModelChoice(model="groq:qwen3-32b", power=settings.power))

    assert NorthSettings(path).default_model.power is StrategyMode.ECO


def test_the_old_single_model_becomes_the_default_row(tmp_path: Path) -> None:
    """Before #80 one model answered everything; it must keep doing so after the update."""
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"power": "sport", "routing": {"mode": "manual", "model": "anthropic:claude-sonnet-5-5"}}),
        encoding="utf-8",
    )
    settings = NorthSettings(path)

    assert settings.default_model == ModelChoice(model="anthropic:claude-sonnet-5-5", power=StrategyMode.SPORT)
    assert all(settings.model_for(kind).model == "anthropic:claude-sonnet-5-5" for kind in WorkKind)


def test_an_old_auto_setting_keeps_its_power_and_drops_the_unused_model(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"power": "eco", "routing": {"mode": "auto", "model": "x:y"}}), encoding="utf-8")

    assert NorthSettings(path).default_model == ModelChoice(power=StrategyMode.ECO)


def test_an_unreadable_row_is_dropped_not_the_whole_file(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "autonomy": "yolo",
                "routing": {
                    "default": {"model": "groq:qwen3-32b", "power": "cruise"},
                    "kinds": {
                        "checks": {"power": "warp"},
                        "nonsense": {"power": "eco"},
                        "background": {"power": "eco"},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    settings = NorthSettings(path)

    assert settings.autonomy.value == "yolo"
    assert settings.default_model.model == "groq:qwen3-32b"
    assert settings.kind_model(WorkKind.CHECKS) is None
    assert settings.kind_model(WorkKind.BACKGROUND) == ModelChoice(power=StrategyMode.ECO)


def test_per_part_overrides_still_load_beside_the_rows(tmp_path: Path) -> None:
    """Both live under "routing", and neither may swallow the other."""
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"routing": {"mode": "manual", "model": "m", "parts": {"coder": {"order_by": "coding_score"}}}}),
        encoding="utf-8",
    )
    settings = NorthSettings(path)
    assert settings.default_model.model == "m"
    assert settings.routing_parts == {"coder": {"order_by": "coding_score"}}

    settings.set_default_model(ModelChoice(model="n"))  # a save must keep the parts it did not touch
    assert json.loads(path.read_text())["routing"]["parts"] == {"coder": {"order_by": "coding_score"}}
