"""The loopback MCP route a coding agent's `ask_north` tool calls (see coding_agents/ask.py).

Like the gate route it is not behind the API secret: the caller is a process the daemon started, holding only
a token that names one run and stops working when that run ends. No token, or a token no run holds, is refused.
It speaks MCP's streamable HTTP in the one shape a tool server needs: a POST with a JSON-RPC message in, the
JSON-RPC reply out, and nothing to say for a notification.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from coding_agents.ask import handle
from orchestrator.api_context import bind_request_services, current_services

coding_ask_router = APIRouter(
    prefix="/orchestrator",
    tags=["coding"],
    dependencies=[Depends(bind_request_services)],
)


@coding_ask_router.post("/coding/ask")
async def coding_ask(request: Request, authorization: str = Header(default="")) -> Response:
    """One MCP message from a coding run: list the one tool, or answer a question put to north."""
    services = current_services()
    token = authorization.removeprefix("Bearer ").strip()
    session = services.require("coding_sessions").lookup(token)
    if session is None:
        raise HTTPException(status_code=403, detail="No coding run holds this token.")
    try:
        message = await request.json()
    except ValueError:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "the body is not JSON"}},
            status_code=400,
        )
    reply = await handle(message, session, services.require("coding_asker"))
    return Response(status_code=202) if reply is None else JSONResponse(reply)
