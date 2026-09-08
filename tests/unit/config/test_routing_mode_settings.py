"""The routing dial: who picks the model, persisted across restarts."""

from __future__ import annotations

import json
from pathlib import Path

from config.strategy import NorthSettings, RoutingMode


def test_defaults_to_auto(tmp_path: Path) -> None:
    settings = NorthSettings(tmp_path / "settings.json")
    assert settings.routing_mode is RoutingMode.AUTO
    assert settings.pinned_model == ""


def test_a_pin_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    NorthSettings(path).set_routing(mode=RoutingMode.MANUAL, model="groq:qwen3-32b")

    reloaded = NorthSettings(path)
    assert reloaded.routing_mode is RoutingMode.MANUAL
    assert reloaded.pinned_model == "groq:qwen3-32b"


def test_the_chosen_model_is_kept_while_auto_is_in_force(tmp_path: Path) -> None:
    """Switching to auto and back must not make the user find their model again."""
    path = tmp_path / "settings.json"
    settings = NorthSettings(path)
    settings.set_routing(mode=RoutingMode.MANUAL, model="groq:qwen3-32b")
    settings.set_routing(mode=RoutingMode.AUTO)

    assert settings.routing_model == "groq:qwen3-32b"
    assert settings.pinned_model == ""  # remembered, not applied
    assert NorthSettings(path).routing_model == "groq:qwen3-32b"


def test_an_unreadable_mode_falls_back_to_auto(tmp_path: Path) -> None:
    """North choosing a model is always a working answer; a half-read pin is not."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"routing": {"mode": "whatever", "model": "x"}}), encoding="utf-8")
    assert NorthSettings(path).routing_mode is RoutingMode.AUTO


def test_per_part_overrides_still_load_beside_the_mode(tmp_path: Path) -> None:
    """Both live under "routing", and neither may swallow the other."""
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"routing": {"mode": "manual", "model": "m", "parts": {"coder": {"order_by": "coding_score"}}}}),
        encoding="utf-8",
    )
    settings = NorthSettings(path)
    assert settings.routing_mode is RoutingMode.MANUAL
    assert settings.routing_parts == {"coder": {"order_by": "coding_score"}}

    settings.set_routing(model="n")  # a save must keep the parts it did not touch
    assert json.loads(path.read_text())["routing"]["parts"] == {"coder": {"order_by": "coding_score"}}
