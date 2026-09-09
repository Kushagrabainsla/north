"""Live user settings: the routing, power and autonomy dials."""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel

from approval.mode import parse_approval_mode
from config.strategy import NorthSettings, RoutingMode, StrategyMode
from orchestrator.api.deps import router
from orchestrator.api_context import current_services


class SettingsOut(BaseModel):
    power: str
    autonomy: str
    # Who picks the model, and - under "manual" - which one, as
    # "provider:model_id". `model` is reported whether or not it is in force, so
    # switching back to manual does not make the user find their model again.
    routing: str = "auto"
    model: str = ""


class SettingsUpdate(BaseModel):
    # Preferred dial names.
    power: str | None = None
    autonomy: str | None = None
    routing: str | None = None
    model: str | None = None


def _settings_out(settings_obj: NorthSettings | None) -> SettingsOut:
    """Render the dials, falling back to the documented defaults when unwired."""
    return SettingsOut(
        power=settings_obj.power.value if settings_obj else "cruise",
        autonomy=settings_obj.autonomy.value if settings_obj else "interactive",
        routing=settings_obj.routing_mode.value if settings_obj else "auto",
        model=settings_obj.routing_model if settings_obj else "",
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

    if body.routing is not None or body.model is not None:
        mode: RoutingMode | None = None
        if body.routing is not None:
            try:
                mode = RoutingMode(body.routing)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail=f"Unknown routing {body.routing!r}. Valid: auto, manual",
                ) from None
        # Manual routing with nothing named would route nothing at all, so it is
        # refused here rather than failing on the next call the user makes.
        pending_model = body.model if body.model is not None else (settings_obj.routing_model if settings_obj else "")
        if mode is RoutingMode.MANUAL and not (pending_model or "").strip():
            raise HTTPException(status_code=422, detail="Manual routing needs a model. Send one as provider:model_id.")
        if settings_obj is not None:
            settings_obj.set_routing(mode, body.model)

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
