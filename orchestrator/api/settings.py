"""Live user settings: the Models panel, and the autonomy and time zone dials."""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel, Field

from config.approval_mode import ApprovalMode, mode_options, require_approval_mode
from config.strategy import ModelChoice, NorthSettings, StrategyMode, WorkKind, describe_kind
from orchestrator.api.deps import router
from orchestrator.api_context import current_services
from utils.time import configure_timezone, format_local, now_epoch, timezone_names

# The key of the Models panel's first row; every other key is a kind of work.
DEFAULT_ROW = "default"


class ModeOption(BaseModel):
    value: str
    description: str


class ModelRow(BaseModel):
    """One row: a model as "provider:model_id", or "" for Auto, which ranks by `power`."""

    model: str = ""
    power: str = StrategyMode.CRUISE.value


class KindRow(BaseModel):
    kind: str
    label: str
    description: str
    # None is "Same as default".
    choice: ModelRow | None = None


class ModelsOut(BaseModel):
    default: ModelRow = Field(default_factory=ModelRow)
    kinds: list[KindRow] = Field(default_factory=list)


class SettingsOut(BaseModel):
    # The Default row's power, which `/power` and "switch to eco mode" change.
    power: str
    autonomy: str
    # The approval modes, from the one definition every surface shows.
    autonomy_options: list[ModeOption] = Field(default_factory=list)
    models: ModelsOut = Field(default_factory=ModelsOut)
    timezone: str = "UTC"
    timezone_options: list[str] = Field(default_factory=list)
    local_time: str = ""
    timezone_configured: bool = False


class SettingsUpdate(BaseModel):
    power: str | None = None
    autonomy: str | None = None
    # Rows to change, by "default" or a kind of work. A kind sent as null is "Same as default".
    models: dict[str, ModelRow | None] | None = None
    timezone: str | None = None
    initialize_timezone_only: bool = False


def _row(choice: ModelChoice) -> ModelRow:
    return ModelRow(model=choice.model, power=choice.power.value)


def _models_out(settings_obj: NorthSettings | None) -> ModelsOut:
    rows = []
    for kind in WorkKind:
        label, description = describe_kind(kind)
        choice = settings_obj.kind_model(kind) if settings_obj else None
        row = _row(choice) if choice is not None else None
        rows.append(KindRow(kind=kind.value, label=label, description=description, choice=row))
    return ModelsOut(default=_row(settings_obj.default_model if settings_obj else ModelChoice()), kinds=rows)


def _settings_out(settings_obj: NorthSettings | None) -> SettingsOut:
    """Render the dials, falling back to the documented defaults when unwired."""
    return SettingsOut(
        power=settings_obj.power.value if settings_obj else "cruise",
        autonomy=settings_obj.autonomy.value if settings_obj else ApprovalMode.ASK.value,
        autonomy_options=[ModeOption(**option) for option in mode_options()],
        models=_models_out(settings_obj),
        timezone=settings_obj.timezone if settings_obj else "UTC",
        timezone_options=timezone_names(),
        local_time=format_local(now_epoch()),
        timezone_configured=settings_obj.timezone_configured if settings_obj else False,
    )


def _power(value: str) -> StrategyMode:
    try:
        return StrategyMode(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Unknown power {value!r}. Valid: eco, cruise, sport") from None


def _model_changes(rows: dict[str, ModelRow | None]) -> list[tuple[WorkKind | None, ModelChoice | None]]:
    """Every row checked before any is saved, so a bad row cannot leave the panel half changed."""
    changes: list[tuple[WorkKind | None, ModelChoice | None]] = []
    for key, row in rows.items():
        if key == DEFAULT_ROW:
            if row is None:
                raise HTTPException(status_code=422, detail="The default row needs a model, or none for Auto.")
            changes.append((None, ModelChoice(model=row.model.strip(), power=_power(row.power))))
            continue
        try:
            kind = WorkKind(key)
        except ValueError:
            valid = ", ".join([DEFAULT_ROW, *WorkKind])
            raise HTTPException(status_code=422, detail=f"Unknown row {key!r}. Valid: {valid}") from None
        choice = None if row is None else ModelChoice(model=row.model.strip(), power=_power(row.power))
        changes.append((kind, choice))
    return changes


@router.get("/settings", response_model=SettingsOut)
async def get_settings() -> SettingsOut:
    """Return current user settings."""
    return _settings_out(current_services().north_settings)


@router.post("/settings", response_model=SettingsOut)
async def update_settings(body: SettingsUpdate) -> SettingsOut:
    """Update user settings live. No restart needed."""
    settings_obj = current_services().north_settings
    # Validate first so a request that also changes another dial cannot be half
    # applied before a bad zone is discovered.
    if body.timezone is not None and not (
        body.initialize_timezone_only and settings_obj is not None and settings_obj.timezone_configured
    ):
        try:
            if settings_obj is None:
                configure_timezone(body.timezone)
            else:
                settings_obj.set_timezone(body.timezone)
                configure_timezone(settings_obj.timezone)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        if (cron_store := current_services().cron_store) is not None:
            await cron_store.zone_changed()
    if body.power is not None:
        mode = _power(body.power)
        if settings_obj is not None:
            settings_obj.set_power(mode)

    if body.models is not None:
        for kind, choice in _model_changes(body.models):
            if settings_obj is None:
                continue
            if kind is None and choice is not None:
                settings_obj.set_default_model(choice)
            elif kind is not None:
                settings_obj.set_kind_model(kind, choice)

    if body.autonomy is not None:
        try:
            approval_mode = require_approval_mode(body.autonomy)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        if settings_obj is not None:
            settings_obj.set_autonomy(approval_mode)

    return _settings_out(settings_obj)
