"""The loopback route a coding agent's hook asks before each action (see coding_agents/hook.py).

It is not behind the API secret: the caller is a process the daemon started, holding only a token that
names one run and stops working when that run ends. No token, or a token no run holds, is refused.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from coding_agents.gate import hook_response
from orchestrator.api_context import bind_request_services, current_services

coding_gate_router = APIRouter(
    prefix="/orchestrator",
    tags=["coding"],
    dependencies=[Depends(bind_request_services)],
)


@coding_gate_router.post("/coding/gate")
async def coding_gate(request: Request, x_gate_token: str = Header(default="")) -> dict:
    """Rule on one tool call of a coding run: allow, deny, or pass (let the agent's own rules decide)."""
    services = current_services()
    session = services.require("coding_sessions").lookup(x_gate_token)
    if session is None:
        raise HTTPException(status_code=403, detail="No coding run holds this token.")
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="The body is not JSON.") from None
    return hook_response(await services.require("coding_gate").decide(session, payload))
