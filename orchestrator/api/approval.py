"""Receive an approval decision and steer a running task."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from orchestrator.api.deps import _get_orchestrator, router


class ApprovalResponse(BaseModel):
    card_id: str
    decision: str
    chosen_option: str = ""
    # Field values as the user left them, for a card that carries work. Merged
    # against the issued card rather than trusted: unknown names are dropped and
    # read-only fields keep what north put there, so an approval can only ever
    # approve what was actually shown.
    values: dict[str, Any] = Field(default_factory=dict)
    # Why, for a rejection. Without one a rejection says only "no", which cannot
    # be learned from - and this is the highest-quality signal north gets.
    reason: str = Field(default="", max_length=2000)
    # Legacy fields - ignored. The decision binds to the server-issued card:
    # task_id and agent are read from the stored card, never trusted from the client.
    task_id: str = ""
    agent: str = ""


@router.post("/approval/respond", status_code=204)
async def respond_approval(body: ApprovalResponse) -> None:
    """Receive an approval decision from the notification callback server or Web UI.

    The card_id must reference a pending card issued by this server; the
    task/agent identity comes from that card, not the request body.
    """
    try:
        await _get_orchestrator().respond_approval(
            card_id=body.card_id,
            decision=body.decision,
            chosen_option=body.chosen_option,
            values=body.values,
            reason=body.reason,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


class SteerRequest(BaseModel):
    task_id: str = ""
    instruction: str


@router.post("/steer")
async def steer_task(body: SteerRequest) -> dict:
    """Submit an in-flight steering directive to an active task."""
    orch = _get_orchestrator()
    task_id = body.task_id
    if not task_id:
        active = list(orch._active_tasks.keys())
        if not active:
            raise HTTPException(status_code=404, detail="No active task to steer.")
        task_id = active[-1]

    await orch.emit_steer(task_id, body.instruction)
    return {"status": "ok", "task_id": task_id, "instruction": body.instruction}
