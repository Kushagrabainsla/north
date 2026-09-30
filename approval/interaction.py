"""Single mediator for every user-facing card interaction.

`UserInteraction` is the one place north turns a `Card` into a user interaction:
decision cards are registered in the `ApprovalStore`, while information cards
go only to notification channels. For decisions it then blocks until the user
responds - however long that takes (CODING_STYLE §13.5: a card never expires).

Tools, flows, agents and the Orchestrator all reach the one instance through
`Approvals`. The surface -> await -> resolve sequence exists exactly once
(DRY / SRP). A missing dependency simply means that channel is skipped.

See docs/CODING_STYLE.md Section 15.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from approval.models import ApprovalDecision, Card, CardField, CardType
from approval.policy import Action, ActionKind, Answer, Verdict
from config.approval_mode import approve_option
from ledger import LedgerEntry, LedgerSource, LedgerStatus

if TYPE_CHECKING:
    from approval.base import Notifier
    from approval.policy import ApprovalPolicy
    from approval.store import ApprovalStore
    from ledger import LedgerWriter

logger = logging.getLogger(__name__)


# Default choices for an approval card when the caller supplies none.
APPROVAL_DEFAULT_OPTIONS: tuple[str, str] = ("Approve", "Reject")

_PENDING = "pending"


class CardEvent(StrEnum):
    """SSE event name a card interaction emits to connected clients."""

    APPROVAL = "approval_required"
    QUESTION = "question_required"


# Per-event key the card body is sent under - clients read these exact keys.
_EVENT_BODY_KEY: dict[CardEvent, str] = {
    CardEvent.APPROVAL: "message",
    CardEvent.QUESTION: "question",
}


class UserInteraction:
    """The single entry point for surfacing cards and awaiting user decisions.

    The composition root builds one instance for tools, flows, agents and the
    orchestrator alike. Absent dependencies (in tests) are skipped, never
    reimplemented elsewhere.
    """

    def __init__(
        self,
        store: ApprovalStore,
        *,
        notifier: Notifier | None = None,
        stream_manager: Any | None = None,
        policy: ApprovalPolicy | None = None,
        ledger: LedgerWriter | None = None,
    ) -> None:
        self._store = store
        self._notifier = notifier
        self._stream = stream_manager
        self._policy = policy
        self._ledger = ledger

    async def request_approval(
        self,
        *,
        task_id: str | None,
        agent: str,
        title: str,
        message: str,
        options: tuple[str, ...] | list[str] = APPROVAL_DEFAULT_OPTIONS,
    ) -> bool:
        """Surface an APPROVAL card and block; return True only if approved."""
        status = await self.request_approval_status(
            task_id=task_id, agent=agent, title=title, message=message, options=options
        )
        return status == ApprovalDecision.APPROVED

    async def request_approval_status(
        self,
        *,
        task_id: str | None,
        agent: str,
        title: str,
        message: str,
        options: tuple[str, ...] | list[str] = APPROVAL_DEFAULT_OPTIONS,
    ) -> ApprovalDecision:
        """Surface an APPROVAL card and block; return how it actually resolved.

        Callers that only need yes/no should use ``request_approval``. This exists
        for the ones that must tell "a person said no" apart from "the task ended
        while the card waited".
        """
        card = self._build(CardType.APPROVAL, task_id, agent, title, message, list(options))
        resolved = await self.request_decision(card, event=CardEvent.APPROVAL)
        return resolved.status

    async def request_work_approval(
        self,
        *,
        task_id: str | None,
        agent: str,
        title: str,
        message: str,
        fields: list[CardField],
        context: str = "",
        options: tuple[str, ...] | list[str] = APPROVAL_DEFAULT_OPTIONS,
    ) -> Card:
        """Hand over filled-in work for a decision; return the resolved card.

        The resolved card carries ``response`` - the field values as the user
        left them, including any edits. Returning the card rather than a bool is
        the whole point: the caller needs what was decided, not only that
        something was.
        """
        card = self._build(CardType.APPROVAL, task_id, agent, title, message, list(options), fields, context)
        return await self.request_decision(card, event=CardEvent.APPROVAL)

    async def hand_over(
        self,
        *,
        task_id: str | None,
        agent: str,
        source: str,
        title: str,
        message: str,
        fields: list[CardField],
        context: str = "",
        options: tuple[str, ...] | list[str] = APPROVAL_DEFAULT_OPTIONS,
    ) -> Card:
        """Leave finished work for the user and return at once, without waiting.

        The counterpart to `request_work_approval`. That one blocks because the
        caller needs the answer to continue; this one is for work that is already
        done - north searched, drafted, and has nothing left to do until you look.
        The task ends, the card stays, and you decide whenever.

        Returns the card as surfaced (already resolved if the policy ruled on it),
        so the caller can record which card it left behind. `source` is what keeps
        the card alive past its task - see `Card.outlives_task`.
        """
        card = self._build(
            CardType.APPROVAL, task_id, agent, title, message, list(options), fields, context
        ).model_copy(update={"blocking": False, "source": source})
        return await self.notify(card, event=CardEvent.APPROVAL)

    async def ask_user(
        self,
        *,
        task_id: str | None,
        agent: str,
        title: str,
        question: str,
        options: list[str],
    ) -> Card:
        """Surface a QUESTION card and block; return the resolved card.

        The user's answer (a chosen option or free text) is on ``chosen_option``.
        """
        card = self._build(CardType.QUESTION, task_id, agent, title, question, list(options))
        return await self.request_decision(card, event=CardEvent.QUESTION)

    async def inform(self, *, task_id: str | None, agent: str, title: str, message: str) -> None:
        """Surface an INFORMATION card. Never blocks."""
        card = self._build(CardType.INFORMATION, task_id, agent, title, message, [])
        await self.notify(card)

    async def remind_waiting(self) -> str:
        """Tell the user what is still waiting on them. Cards never expire, so they are reminded instead."""
        waiting = sorted(self._store.pending(), key=lambda card: card.created_at)
        if not waiting:
            return "nothing is waiting for you"
        now = datetime.now(UTC)
        lines = [f"- {card.title} ({card.agent}), waiting {_waited(now - card.created_at)}" for card in waiting]
        await self.inform(
            task_id=None,
            agent="north",
            title=f"{len(waiting)} waiting for you",
            message="\n".join(lines),
        )
        return f"reminded you of {len(waiting)} waiting card(s)"

    async def request_decision(self, card: Card, *, event: CardEvent | None = None) -> Card:
        """Surface *card* and block until it resolves; return the resolved card.

        A learned rule may resolve it immediately; otherwise it waits for the user.
        """
        surfaced = await self.notify(card, event=event)
        if surfaced.status != _PENDING:
            return surfaced  # auto-resolved by a learned rule
        return await self._await(card)

    async def ask_person(self, card: Card, *, event: CardEvent) -> Card:
        """Show *card* to the user and wait, consulting no rule: the caller already ruled.

        `Approvals` rules on the action's own facts before it gets here, so ruling
        again on the card's text would only second-guess that with less to go on.
        """
        await self._surface(card, event)
        return await self._await(card)

    def record_resolved(self, card: Card, answer: Answer) -> None:
        """Store *card* already decided - an action north took, or answered, without asking - and why."""
        self._store.add(card.model_copy(update={"reason": answer.reason, "memory_used": list(answer.memory_used)}))
        self._store.resolve(card.id, answer.decision, chosen_option=answer.chosen_option, decided_by=answer.decided_by)

    async def _await(self, card: Card) -> Card:
        """Wait for the user's answer, however long it takes (CODING_STYLE §13.5)."""
        resolved = await self._store.wait_for_decision(card.id)
        return resolved or self._store.get(card.id) or card

    async def notify(self, card: Card, *, event: CardEvent | None = None) -> Card:
        """Surface *card* without blocking; register only decision cards.

        The returned card is already resolved if a learned rule fired. Surfacing
        skips whichever channel is absent: SSE only when an *event* and a stream
        manager are present; a system alert only when a Notifier is wired.

        INFORMATION is deliberately a separate path: it has no response, waiter,
        or approval history. Treating a completion notice as a pending approval
        made finished tasks appear to be waiting for the user and then cancelled
        those notices when the task ended.
        """
        if card.type is CardType.INFORMATION:
            notification = card.model_copy(update={"blocking": False}).scrubbed()
            if self._notifier is not None:
                await self._notifier.notify(notification)
            return notification

        if card.type is CardType.APPROVAL and not card.action_key:
            card = card.model_copy(update={"action_key": _action_for(card).describe()})
        auto = await self._auto_resolve(card)
        if auto is not None:
            return auto
        await self._surface(card, event)
        return card

    async def _surface(self, card: Card, event: CardEvent | None) -> None:
        """Register *card* as waiting and show it on every channel that is wired."""
        self._store.add(card)
        if event is not None:
            await self._emit(card, event)
        if self._notifier is not None:
            await self._notifier.notify(card)

    async def _auto_resolve(self, card: Card) -> Card | None:
        """Resolve *card* without the user, or return None to surface it.

        The policy decides both kinds: a directly-raised APPROVAL card is ruled
        on like any tool action, and a QUESTION is answered only in the modes
        that answer for you (YOLO, Autonomous). Neither path may block the user
        from being asked: any error logs and falls through to surfacing the card.
        """
        if self._policy is None:
            return None
        try:
            if card.type is CardType.APPROVAL:
                answer = await self._rule_on(card)
            elif card.type is CardType.QUESTION:
                answer = await self._policy.answer(card)
            else:
                answer = None
        except Exception:
            logger.debug("ApprovalPolicy failed for card %s - surfacing it", card.id)
            return None
        if answer is None:
            return None
        self.record_resolved(card, answer)
        await self._audit(card, answer)
        return card.model_copy(
            update={
                "status": answer.decision,
                "chosen_option": answer.chosen_option,
                "decided_by": answer.decided_by,
                "reason": answer.reason,
                "memory_used": list(answer.memory_used),
            }
        )

    async def _audit(self, card: Card, answer: Answer) -> None:
        """Ledger a decision north took for you, exactly as a decision of yours is ledgered."""
        logger.info("ApprovalPolicy: auto-%s card %s (%s)", answer.decision, card.id, answer.reason)
        if self._ledger is None:
            return
        await self._ledger.write(
            LedgerEntry.new(
                source=LedgerSource.APPROVAL,
                task_id=card.task_id,
                agent=card.agent,
                action=f"auto_{answer.decision}",
                input=card.title,
                output=f"{answer.chosen_option or answer.decision} - {answer.reason}",
                status=LedgerStatus.COMPLETED,
            )
        )

    async def _rule_on(self, card: Card) -> Answer | None:
        """The policy's ruling on a directly-raised approval card, or None to ask."""
        ruling = await self._policy.rule(_action_for(card))
        if ruling.verdict is Verdict.ASK:
            return None
        if ruling.allowed:
            chosen = approve_option(card.options)
            return Answer(ApprovalDecision.APPROVED, chosen, ruling.rule, ruling.decided_by, ruling.memory_used)
        return Answer(ApprovalDecision.REJECTED, "", ruling.rule, ruling.decided_by, ruling.memory_used)

    async def _emit(self, card: Card, event: CardEvent) -> None:
        if self._stream is None or not card.task_id:
            return
        await self._stream.emit(
            card.task_id,
            event.value,
            {
                "card_id": card.id,
                "task_id": card.task_id,
                "agent": card.agent,
                "title": card.title,
                _EVENT_BODY_KEY[event]: card.message,
                "options": card.options,
                # Sent even when empty so a client can tell a card that carries
                # work from one that is only a question, without re-fetching it.
                "fields": [field.model_dump(mode="json") for field in card.fields],
                "context": card.context,
                "blocking": card.blocking,
                "source": card.source,
            },
        )

    @staticmethod
    def _build(
        card_type: CardType,
        task_id: str | None,
        agent: str,
        title: str,
        body: str,
        options: list[str],
        fields: list[CardField] | None = None,
        context: str = "",
    ) -> Card:
        return Card.new(
            type=card_type,
            task_id=task_id,
            agent=agent,
            title=title,
            message=body,
            options=options,
            fields=fields or [],
            context=context,
        )


def _waited(elapsed: timedelta) -> str:
    """How long a card has waited, in the largest whole unit."""
    hours = int(elapsed.total_seconds() // 3600)
    if hours >= 48:
        return f"{hours // 24} days"
    if hours >= 1:
        return f"{hours}h"
    return f"{int(elapsed.total_seconds() // 60)}m"


def _action_for(card: Card) -> Action:
    """The action a directly-raised approval card asks about.

    One definition, used both to rule on the card and to stamp its
    `action_key`, so the key a decision is learned under is the key the policy
    recalls it by.
    """
    return Action(
        agent=card.agent,
        kind=ActionKind.OTHER,
        summary=card.title,
        command=card.message,
        carries_work=bool(card.fields),
        details=_work_details(card),
    )


def _work_details(card: Card) -> str:
    """The filled-in fields and their source material, as the memory decider reads them."""
    lines = [f"{field.label or field.name}: {field.value}" for field in card.fields]
    if card.context:
        lines.append(f"Source material:\n{card.context[:4000]}")
    return "\n".join(lines)
