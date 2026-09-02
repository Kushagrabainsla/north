"""Live user settings: the power and autonomy dials."""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel

from approval.mode import parse_approval_mode
from config.strategy import NorthSettings, StrategyMode
from orchestrator.api.deps import router
from orchestrator.api_context import current_services


class SettingsOut(BaseModel):
    power: str
    autonomy: str


class SettingsUpdate(BaseModel):
    # Preferred dial names.
    power: str | None = None
    autonomy: str | None = None


def _settings_out(settings_obj: NorthSettings | None) -> SettingsOut:
    """Render the dials, falling back to the documented defaults when unwired."""
    return SettingsOut(
        power=settings_obj.power.value if settings_obj else "cruise",
        autonomy=settings_obj.autonomy.value if settings_obj else "interactive",
    )


@router.get("/settings", response_model=SettingsOut)
async def get_settings() -> SettingsOut:
    """Return current user settings."""
    return _settings_out(current_services().north_settings)


@router.post("/settings", response_model=SettingsOut)
async def update_settings(body: SettingsUpdate) -> SettingsOut:
    """Update user settings live (power and/or autonomy). No restart needed."""
    settings_obj = current_services().north_settings
    if body.power is not None:
        try:
            mode = StrategyMode(body.power)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown power {body.power!r}. Valid: eco, cruise, sport",
            ) from None
        if settings_obj is not None:
            settings_obj.set_power(mode)

    if body.autonomy is not None:
        approval_mode = parse_approval_mode(body.autonomy)
        if approval_mode is None:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown autonomy {body.autonomy!r}. Valid: interactive, auto, autonomous",
            ) from None
        if settings_obj is not None:
            settings_obj.set_autonomy(approval_mode)

    return _settings_out(settings_obj)


