"""Gets a coding agent's question answered through north's one approval layer.

A coding agent calls `ask_north` (`coding_agents/ask.py`); this turns the call into a question card. Who
answers it is the approval mode's business and nothing here: in ask and safe mode the user is asked, in
autonomous mode the memory decider answers from what north knows, in yolo the usual yes. Nothing in this file
decides (CODING_STYLE 7.3); it describes the question, waits, and tells the run what happened.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Mapping
from typing import Any, Protocol

from approval.interaction import CardEvent, UserInteraction
from approval.models import Card, CardType, DecidedBy
from coding_agents import GateSession
from coding_agents.ask import Question, Reply

AGENT = "coding_agent"
# The most questions one run may ask: past it the agent is told to use its own judgement and say so.
MAX_ASKS_PER_RUN = 20
# A run that is answered at once should not flicker on the dashboard as waiting.
_WAITING_AFTER_SECONDS = 0.5
_RECORDED_CHARS = 300

_NO_ANSWER = (
    "No answer came back. Do not guess silently: choose the safest reasonable option, and say in your final "
    "report exactly what you assumed and why."
)
_TOO_MANY = (
    f"You have asked north {MAX_ASKS_PER_RUN} questions in this run, which is the limit. Use your own judgement "
    "for the rest, and say in your final report what you assumed."
)
_BY: Mapping[str, str] = {
    DecidedBy.YOU.value: "The user",
    DecidedBy.MEMORY_DECIDER.value: "North, answering for the user from what it knows about them,",
    DecidedBy.YOLO.value: "North (yolo mode: everything is yes)",
}


class RunLog(Protocol):
    """Where a run is shown to be waiting, and where what happened to it is written."""

    async def waiting(self, run_id: str, waiting: bool) -> None: ...

    async def record(self, run_id: str, task_id: str, event: str, data: Mapping[str, Any]) -> None: ...


class ApprovalsAsker:
    """A coding agent's questions, as question cards."""

    def __init__(self, interaction: UserInteraction, log: RunLog, *, max_asks: int = MAX_ASKS_PER_RUN) -> None:
        self._interaction = interaction
        self._log = log
        self._max_asks = max_asks
        self._asked: Counter[str] = Counter()

    async def ask(self, session: GateSession, question: Question) -> Reply:
        self._asked[session.run_id] += 1
        if self._asked[session.run_id] > self._max_asks:
            await self._log.record(session.run_id, session.task_id, "ask_refused", {"reason": "too many questions"})
            return Reply(_TOO_MANY, answered=False)
        card = Card.new(
            type=CardType.QUESTION,
            task_id=session.task_id,
            agent=AGENT,
            title="Coding agent - Question",
            message=question.text,
            options=question.options,
        )
        resolved = await self._wait_for(session, card)
        answer = (resolved.chosen_option or "").strip()
        await self._log.record(
            session.run_id,
            session.task_id,
            "ask",
            {
                "question": question.text[:_RECORDED_CHARS],
                "answer": answer[:_RECORDED_CHARS],
                "by": resolved.decided_by or DecidedBy.YOU.value,
                "card": card.id,
            },
        )
        if not answer:
            return Reply(_NO_ANSWER, answered=False)
        by = _BY.get(resolved.decided_by or DecidedBy.YOU.value, "North")
        why = resolved.reason if resolved.decided_by == DecidedBy.MEMORY_DECIDER.value else ""
        return Reply(answer, by=by, reason=why)

    async def _wait_for(self, session: GateSession, card: Card) -> Card:
        shown = {"waiting": False}

        async def show_waiting_soon() -> None:
            await asyncio.sleep(_WAITING_AFTER_SECONDS)
            shown["waiting"] = True
            await self._log.waiting(session.run_id, True)

        marker = asyncio.create_task(show_waiting_soon())
        try:
            return await self._interaction.request_decision(card, event=CardEvent.QUESTION)
        finally:
            marker.cancel()
            await asyncio.gather(marker, return_exceptions=True)
            if shown["waiting"]:
                await self._log.waiting(session.run_id, False)
