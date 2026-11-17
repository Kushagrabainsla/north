"""The settings endpoint the web page drives for the Models panel (#80)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import orchestrator.api.settings as api
from config.strategy import ModelChoice, NorthSettings, StrategyMode, WorkKind
from orchestrator.api_context import ApiServices, bind_services


@pytest.fixture
def settings(tmp_path) -> NorthSettings:
    return NorthSettings(tmp_path / "settings.json")


async def update(**body):
    return await api.update_settings(api.SettingsUpdate(**body))


@pytest.mark.asyncio
async def test_the_panel_lists_the_default_row_and_every_kind(settings) -> None:
    with bind_services(ApiServices(north_settings=settings)):
        out = await api.get_settings()

    assert out.models.default == api.ModelRow(model="", power="cruise")
    assert [row.kind for row in out.models.kinds] == ["tasks", "checks", "approvals", "background"]
    assert [row.label for row in out.models.kinds] == ["Your tasks", "Checks", "Approvals", "Background"]
    assert all(row.choice is None for row in out.models.kinds)  # "Same as default"


@pytest.mark.asyncio
async def test_rows_are_changed_by_name(settings) -> None:
    with bind_services(ApiServices(north_settings=settings)):
        out = await update(
            models={
                "default": {"model": "groq:qwen3-32b"},
                "background": {"model": "", "power": "eco"},
            }
        )

    assert out.models.default.model == "groq:qwen3-32b"
    background = next(row for row in out.models.kinds if row.kind == "background")
    assert background.choice == api.ModelRow(model="", power="eco")
    assert settings.model_for(WorkKind.TASKS).model == "groq:qwen3-32b"
    assert settings.model_for(WorkKind.BACKGROUND) == ModelChoice(power=StrategyMode.ECO)


@pytest.mark.asyncio
async def test_a_kind_sent_as_null_is_same_as_default_again(settings) -> None:
    settings.set_kind_model(WorkKind.CHECKS, ModelChoice(power=StrategyMode.SPORT))
    with bind_services(ApiServices(north_settings=settings)):
        await update(models={"checks": None})
    assert settings.kind_model(WorkKind.CHECKS) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "models",
    [
        {"default": None},
        {"sideways": {"model": ""}},
        {"checks": {"model": "", "power": "warp"}},
        # Checked before anything is saved: the good row is not applied either.
        {"tasks": {"model": "groq:qwen3-32b"}, "nonsense": None},
    ],
)
async def test_a_bad_row_is_a_422_and_changes_nothing(settings, models) -> None:
    with bind_services(ApiServices(north_settings=settings)), pytest.raises(HTTPException) as exc:
        await update(models=models)
    assert exc.value.status_code == 422
    assert settings.default_model == ModelChoice()
    assert all(settings.kind_model(kind) is None for kind in WorkKind)


@pytest.mark.asyncio
async def test_power_sets_the_default_row(settings) -> None:
    """What `/power eco` and "switch to eco mode" send."""
    with bind_services(ApiServices(north_settings=settings)):
        out = await update(power="eco")
    assert out.power == "eco" and out.models.default.power == "eco"


@pytest.mark.asyncio
async def test_timezone_is_reported_and_changed_live(settings, monkeypatch) -> None:
    import utils.time as time_utils

    monkeypatch.setattr(time_utils, "_configured_timezone_name", None)
    with bind_services(ApiServices(north_settings=settings)):
        out = await update(timezone="America/Los_Angeles")

    assert out.timezone == "America/Los_Angeles"
    assert "America/Los_Angeles" in out.timezone_options
    assert out.local_time.endswith(("PDT", "PST"))
    assert time_utils.local_timezone_name() == "America/Los_Angeles"


@pytest.mark.asyncio
async def test_unknown_timezone_is_a_422(settings) -> None:
    with bind_services(ApiServices(north_settings=settings)), pytest.raises(HTTPException) as exc:
        await update(timezone="Mars/Olympus_Mons")

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_the_catalog_is_grouped_by_provider(tmp_path) -> None:
    """What the manual picker offers: provider first, then that provider's models."""
    import orchestrator.api.inference as inference_api
    from inference.models import ModelEntry, ModelGroup

    class _Router:
        def models_by_capability(self):
            # The same model appears in several pools; the picker must list it once.
            entries = [ModelEntry(id="qwen3-32b", provider="groq"), ModelEntry(id="gpt-5.6-sol", provider="codex")]
            return {
                "reasoning": ModelGroup(name="reasoning", models=entries),
                "speed": ModelGroup(name="speed", models=[entries[0]]),
            }

    with bind_services(ApiServices(inference_router=_Router())):
        catalog = await inference_api.inference_catalog()

    assert [row.provider for row in catalog] == ["codex", "groq"]  # sorted, for a stable list
    assert [row.models for row in catalog] == [["gpt-5.6-sol"], ["qwen3-32b"]]
