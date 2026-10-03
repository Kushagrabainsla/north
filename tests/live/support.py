"""A real daemon route for the live tests: the gate router in front of the real approval layer, over loopback."""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field

import uvicorn
from fastapi import FastAPI

from approval.models import ApprovalDecision
from approval.store import ApprovalStore
from coding_agents import Gate, GateSessions, ToolRequest, Verdict
from config.approval_mode import ApprovalMode
from orchestrator.api import coding_gate_router
from orchestrator.api_context import ApiServices, attach
from orchestrator.coding_gate import ApprovalJudge
from tests.conftest import approvals


class _Statuses:
    async def waiting(self, run_id: str, waiting: bool) -> None:
        return None


class _CountingJudge:
    """The real judge, remembering what it was asked."""

    def __init__(self, inner: ApprovalJudge) -> None:
        self._inner = inner
        self.asked: list[ToolRequest] = []

    async def judge(self, session, request, *, inside_worktree):
        self.asked.append(request)
        return await self._inner.judge(session, request, inside_worktree=inside_worktree)


@dataclass
class LiveGate:
    sessions: GateSessions
    store: ApprovalStore
    judge: _CountingJudge
    url: str
    cards: list = field(default_factory=list)
    _server: uvicorn.Server | None = None
    _serving: asyncio.Task | None = None
    _answering: asyncio.Task | None = None

    async def answer_cards(self, decision: ApprovalDecision) -> None:
        """Answer every card as it appears, the way a person at the dashboard would."""
        while True:
            for card in self.store.pending():
                self.cards.append(card)
                self.store.resolve(card.id, decision)
            await asyncio.sleep(0.02)

    async def stop(self) -> None:
        for task in (self._answering,):
            if task:
                task.cancel()
        if self._server:
            self._server.should_exit = True
        if self._serving:
            await self._serving


async def start_gate(mode: ApprovalMode, answer: ApprovalDecision | None = None) -> LiveGate:
    store = ApprovalStore()
    sessions = GateSessions()
    judge = _CountingJudge(ApprovalJudge(approvals(mode, store=store), _Statuses()))
    app = FastAPI()
    attach(app, ApiServices(coding_sessions=sessions, coding_gate=Gate(judge)))
    app.include_router(coding_gate_router)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    gate = LiveGate(sessions, store, judge, f"http://127.0.0.1:{port}/orchestrator/coding/gate")
    gate._server, gate._serving = server, serving
    if answer is not None:
        gate._answering = asyncio.create_task(gate.answer_cards(answer))
    return gate


__all__ = ["LiveGate", "Verdict", "start_gate"]
