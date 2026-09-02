"""Receive an approval decision and steer a running task."""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel

from orchestrator.api.deps import _get_orchestrator, router


class ApprovalResponse(BaseModel):
    card_id: str
    decision: str
    chosen_option: str = ""
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
