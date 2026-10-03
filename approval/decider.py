"""The memory decider: how Autonomous answers for you instead of asking.

Autonomous never asks (CODING_STYLE §7.3). It used to mean "allow everything",
with no model and nothing learned from you - so it could not tell a change you
would want from one you would not. Now a model reads what north knows about you
and gives the answer that best fits you, with a one-line reason and the memory
items that led there, so every decision can be shown and checked afterwards.

What it reads, for every request kind (tool actions, questions, prepared work):

- facts about you that match the request, and your profile;
- your judgement rules;
- your past approve/reject decisions for the same agent;
- the facts north itself established about the request.

Text an agent wrote, or that came from outside north, is marked untrusted: it
is judged, never obeyed. The model is picked by the ``approval_decider`` routing
part, so manual routing uses the pinned model like every other call.

When it cannot decide - no model, an error, an unreadable reply - it returns
None and the card waits for you. That is the one case Autonomous pauses.

An action that leaves the sandbox (a push, a host it was not given) is the same:
it is approved only when the reply cites something you stated - a fact, a past
decision, your rules or your profile. Citing nothing is a guess, so the card waits.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from approval.models import ApprovalDecision, Card, DecidedBy, MemoryKind, MemoryRef
from approval.policy import Action, Answer
from inference.models import CompletionRequest
from memory import ContextDocument
from utils.prompts import load_prompt
from utils.text import extract_json

if TYPE_CHECKING:
    from approval.approval_memory import ApprovalMemory
    from inference.base import InferenceRouter
    from memory import MemoryGateway

logger = logging.getLogger(__name__)

COMPONENT = "approval_decider"

_FACT_LIMIT = 10
_EPISODE_LIMIT = 3
_PAST_DECISION_LIMIT = 10
_DOCUMENT_CHARS = 3000
_CONTENT_CHARS = 6000
_QUERY_CHARS = 1000

_ACTION_DECISIONS = {"approve": ApprovalDecision.APPROVED, "reject": ApprovalDecision.REJECTED}
_UNTRUSTED_TAG = re.compile(r"</?\s*untrusted\s*>", re.IGNORECASE)
# What may cover an action that leaves the sandbox. An episode is north's own summary of a
# past task, which an agent's output can shape, so it never authorizes one.
_COVERING_KINDS = frozenset({MemoryKind.FACT, MemoryKind.PAST_DECISION, MemoryKind.JUDGEMENT_RULES, MemoryKind.PROFILE})


@dataclass(frozen=True)
class _Item:
    """One thing north knows about you, as the model cites it and as the card records it."""

    id: str
    text: str
    recorded_as: MemoryRef


@dataclass(frozen=True)
class _Subject:
    """What is being decided, split into what north established and what an agent wrote."""

    kind: str  # "action", "prepared work" or "question"
    agent: str
    facts: str
    content: str
    options: tuple[str, ...]
    task_id: str = ""
    leaves_sandbox: bool = False


class MemoryDecider:
    """Decides a request for you from your memory. The `Decider` `ApprovalPolicy` consults in Autonomous."""

    def __init__(
        self,
        memory: MemoryGateway,
        inference_router: InferenceRouter,
        approval_memory: ApprovalMemory | None = None,
    ) -> None:
        self._memory = memory
        self._inference_router = inference_router
        self._approval_memory = approval_memory

    async def rule(self, action: Action) -> Answer | None:
        """Approve or reject an action (or prepared work), or None when undecided."""
        subject = _Subject(
            kind="prepared work" if action.carries_work else "action",
            agent=action.agent,
            facts=_action_facts(action),
            content=_action_content(action),
            options=("approve", "reject"),
            leaves_sandbox=action.leaves_sandbox,
        )
        return await self._decide(subject)

    async def answer(self, card: Card) -> Answer | None:
        """Answer a question card, or None when undecided."""
        subject = _Subject(
            kind="question",
            agent=card.agent,
            facts="- a question the agent cannot continue without",
            content=f"{card.title}\n{card.message}",
            options=tuple(card.options),
            task_id=card.task_id,
        )
        return await self._decide(subject)

    async def _decide(self, subject: _Subject) -> Answer | None:
        items = await self._known_items(subject)
        prompt = load_prompt("prompts/approval_decider.md").format(
            memory="\n".join(f"[{item.id}] {item.text}" for item in items) or "(nothing known yet)",
            kind=subject.kind,
            agent=subject.agent,
            facts=subject.facts,
            options=", ".join(subject.options) if subject.options else "none - answer in your own words",
            content=_UNTRUSTED_TAG.sub("", subject.content)[:_CONTENT_CHARS],
        )
        try:
            response = await self._inference_router.complete(
                CompletionRequest(
                    prompt=prompt,
                    component=COMPONENT,
                    task_id=subject.task_id,
                    json_mode=True,
                )
            )
            reply = extract_json(response.text)
        except Exception:
            logger.warning("Memory decider: the model call failed - the card waits for you", exc_info=True)
            return None
        answer = _read_reply(reply, subject, items)
        if answer is not None and _is_a_guess(answer, subject):
            logger.info(
                "Memory decider: nothing you stated covers this action from %s that leaves the sandbox - "
                "the card waits for you",
                subject.agent,
            )
            return None
        if answer is None:
            logger.warning(
                "Memory decider: unreadable reply for a %s from %s - the card waits for you",
                subject.kind,
                subject.agent,
            )
        return answer

    async def _known_items(self, subject: _Subject) -> list[_Item]:
        """Everything north knows about you that bears on *subject*, each with an id to cite."""
        query = f"{subject.agent} {subject.content}"[:_QUERY_CHARS]
        principal = await self._memory.principal_for(COMPONENT)
        recalled = await self._memory.recall(principal, query, fact_limit=_FACT_LIMIT, episode_limit=_EPISODE_LIMIT)
        rules = (await self._memory.read_document(ContextDocument.JUDGEMENT_RULES)).strip()

        items = [
            _Item(f"F{n}", fact, MemoryRef(kind=MemoryKind.FACT, text=fact)) for n, fact in enumerate(recalled.facts, 1)
        ]
        items += [
            _Item(f"E{n}", episode, MemoryRef(kind=MemoryKind.EPISODE, text=episode))
            for n, episode in enumerate(recalled.episodes, 1)
        ]
        items += [_Item(f"D{n}", ref.text, ref) for n, ref in enumerate(self._past_decisions(subject.agent), 1)]
        if rules:
            text = f"Your judgement rules:\n{rules[:_DOCUMENT_CHARS]}"
            items.append(_Item("R1", text, MemoryRef(kind=MemoryKind.JUDGEMENT_RULES)))
        items += [
            _Item(f"P{n}", f"Your profile:\n{document[:_DOCUMENT_CHARS]}", MemoryRef(kind=MemoryKind.PROFILE))
            for n, document in enumerate(recalled.documents, 1)
        ]
        return items

    def _past_decisions(self, agent: str) -> list[MemoryRef]:
        if self._approval_memory is None:
            return []
        rows = [row for row in self._approval_memory.all_decisions() if row.get("agent") == agent]
        return [
            MemoryRef(
                kind=MemoryKind.PAST_DECISION,
                text=f"you {row['decision']} {row['signature']!r} ({row['count']}x)",
                ref=str(row["fingerprint"]),
            )
            for row in rows[:_PAST_DECISION_LIMIT]
        ]


def _action_facts(action: Action) -> str:
    """What north itself established about the action - trusted, unlike anything the agent wrote."""
    flags = {
        "changes something": action.mutating,
        "recognised as one of a few catastrophic shapes": action.obviously_destructive,
        "sends something to a person other than you": action.reaches_third_party,
        "spends money": action.spends_money,
        "leaves the sandbox or the workspace": action.leaves_sandbox,
        "is filled-in work for review": action.carries_work,
    }
    lines = [f"- kind: {action.kind.value}"]
    lines += [f"- {name}" for name, present in flags.items() if present]
    return "\n".join(lines)


def _action_content(action: Action) -> str:
    """The action as the agent described it: every string here came from the model."""
    parts = [action.summary]
    for label, value in (
        ("operation", action.operation),
        ("command", action.command),
        ("args", action.args),
        ("path", str(action.path) if action.path else ""),
        ("details", action.details),
    ):
        if value:
            parts.append(f"{label}: {value}")
    return "\n".join(parts)


def _read_reply(reply: Any, subject: _Subject, items: list[_Item]) -> Answer | None:
    """The model's reply as an `Answer`, or None when it does not say something usable."""
    if not isinstance(reply, dict):
        return None
    reason = str(reply.get("reason") or "").strip()
    if not reason:
        return None
    by_id = {item.id: item for item in items}
    used = tuple(by_id[ref].recorded_as for ref in _cited(reply.get("used")) if ref in by_id)
    decision = str(reply.get("decision") or "").strip().lower()

    if subject.kind != "question":
        verdict = _ACTION_DECISIONS.get(decision)
        return Answer(verdict, "", reason, DecidedBy.MEMORY_DECIDER, used) if verdict is not None else None

    if decision != "answer":
        return None
    option = str(reply.get("option") or "").strip()
    if subject.options:
        option = next((choice for choice in subject.options if choice.lower() == option.lower()), "")
    return Answer(ApprovalDecision.ANSWERED, option, reason, DecidedBy.MEMORY_DECIDER, used) if option else None


def _is_a_guess(answer: Answer, subject: _Subject) -> bool:
    """An approval for an action that leaves the sandbox, with nothing the user stated behind it."""
    if not subject.leaves_sandbox or answer.decision != ApprovalDecision.APPROVED:
        return False
    return not any(ref.kind in _COVERING_KINDS for ref in answer.memory_used)


def _cited(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(ref).strip().upper() for ref in raw]
