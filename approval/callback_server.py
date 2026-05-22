"""Callback server — FastAPI daemon on port 8001.

Receives action decisions from macOS native notifications (via `alerter`)
and relays them to the main Orchestrator on port 8000.

See docs/CODING_STYLE.md Sections 12, 17.
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from approval.models import ApprovalDecision
from config.settings import settings
from utils.security import load_secret, verify_secret


class CallbackPayload(BaseModel):
    """Payload posted by alerter (or the Web UI) when a user makes a decision."""

    card_id: str
    task_id: str
    agent: str
    action: str  # The button label the user clicked, e.g. "Approve", "Reject", or an option


class CallbackResponse(BaseModel):
    """Response returned after processing a callback."""

    received: bool
    card_id: str


app = FastAPI(
    title="north Callback Server",
    description="Receives user action decisions from macOS notifications",
    version="1.0.0",
)

_ORCHESTRATOR_BASE_URL = "http://127.0.0.1:8000"


@app.post("/callback/decision", response_model=CallbackResponse)
async def receive_decision(
    payload: CallbackPayload,
    x_north_secret: str = Header(...),
) -> CallbackResponse:
    """Accept a user decision from a macOS notification callback.

    Verifies the shared secret, maps the clicked action to an
    ApprovalDecision, and relays it to the main Orchestrator.
    """
    if not verify_secret(x_north_secret):
        raise HTTPException(status_code=403, detail="Invalid secret.")

    decision = _map_action_to_decision(payload.action)

    # Forward the decision to the main orchestrator
    secret = load_secret()
    async with httpx.AsyncClient() as client:
        try:
            await client.post(
                f"{_ORCHESTRATOR_BASE_URL}/orchestrator/approval/respond",
                json={
                    "card_id": payload.card_id,
                    "task_id": payload.task_id,
                    "agent": payload.agent,
                    "decision": decision,
                    "chosen_option": payload.action,
                },
                headers={"X-North-Secret": secret},
                timeout=10.0,
            )
        except httpx.RequestError:
            # Log but don't crash — the callback was received, relay is best-effort
            pass

    return CallbackResponse(received=True, card_id=payload.card_id)


@app.get("/callback/health")
async def health_check() -> dict[str, str]:
    """Simple health probe for the callback server."""
    return {"status": "ok"}


def _map_action_to_decision(action: str) -> str:
    """Normalise a raw button label to an ApprovalDecision value."""
    normalised = action.strip().lower()
    if normalised in {"approve", "proceed anyway", "yes", "confirm"}:
        return ApprovalDecision.APPROVED.value
    if normalised in {"reject", "cancel", "no", "deny"}:
        return ApprovalDecision.REJECTED.value
    # For question options — pass through as-is
    return action
