"""The approval layer's one entry point: may north do this?

Every tool call that changes something reaches this class through
`Tool.execute`, so no tool can forget to ask. Before it existed each gated tool
called the gate itself, and twelve mutating tools - the browser, `north_config`
(which could switch the approval mode), schedules, MCP tools - simply never did.

The split is unchanged: the caller describes the action as facts (`Request`),
`ApprovalPolicy` rules on it, and only an ``ASK`` becomes a card for the user.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from approval.interaction import APPROVAL_DEFAULT_OPTIONS, CardEvent, UserInteraction
from approval.models import ApprovalDecision, Card, CardType, DecidedBy
from approval.policy import Action, Answer, ApprovalPolicy, Ruling, Verdict, may_replay_yes
from approval.unattended import forbidden_reason
from config.approval_mode import approve_option

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from approval.effects import EffectLog
    from approval.rejections import RejectionLog

# Why an irreversible action is refused the second time in one task (see approval/effects.py).
_ALREADY_DONE = "already done earlier in this task, and it reaches someone else or spends money, so it is not repeated"
# Why a request is refused when there is nobody to ask it of.
_NOBODY_TO_ASK = "no approval layer is bound to this tool instance, so nobody can be asked (fail closed)"


@dataclass(frozen=True)
class Request:
    """What a caller wants to do: the facts the policy reads, and the card a person sees.

    ``prepared`` is whatever the caller computed to describe the action - a planned
    edit, a parsed command. It is handed back with the approval so the caller acts
    on exactly what was approved rather than recomputing it.
    """

    action: Action
    title: str
    message: str
    options: tuple[str, ...] = APPROVAL_DEFAULT_OPTIONS
    declined: str = "Action cancelled by user."
    refused_hint: str = ""
    prepared: Any = None
    # Editing a user-authored procedure requires fresh consent, even if a
    # remembered or autonomous rule would ordinarily allow this tool.
    requires_confirmation: bool = False


@dataclass(frozen=True)
class Decision:
    """How a request was decided.

    ``status`` is set only when a card was raised - it tells "a person said no"
    apart from "nobody answered". A policy refusal has no status: nobody was asked.
    """

    verdict: Verdict
    reason: str
    status: str | None = None  # the card's status, an ApprovalDecision value
    # Who decided (a DecidedBy value), so a caller never reports north's yes as yours (issue #76, A15).
    decided_by: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW


class Approvals:
    """Rules on a `Request`; asks the user only when the policy says ASK."""

    def __init__(
        self,
        policy: ApprovalPolicy,
        interaction: UserInteraction | None,
        effects: EffectLog | None = None,
        rejections: RejectionLog | None = None,
    ) -> None:
        self._policy = policy
        self._interaction = interaction
        self._effects = effects
        self._rejections = rejections
        # The exact actions a person approved in each task, so the task does not ask again when it retries one:
        # an approved `kasa off` that failed was asked about a second time by the agent that took over (#76, W4).
        # Kept in memory: after a restart north asks again, the safe way to forget.
        self._approved: dict[str, set[str]] = {}
        # The calls a person approved ahead on one card (see decide_ahead), each to run once, by task.
        self._granted: dict[str, Counter[str]] = {}

    @classmethod
    def unbound(cls) -> Approvals:
        """For a tool nobody wired: read-only work runs, anything that would ask is refused.

        The real policy with nobody to ask, rather than a special case - a
        missing gate must never read as an open one.
        """
        return cls(ApprovalPolicy(), None)

    @property
    def interaction(self) -> UserInteraction | None:
        """The card channel, for callers that ask the user something other than an action."""
        return self._interaction

    async def decide(self, request: Request, *, task_id: str | None) -> Decision:
        """Allow, refuse, or ask - and when asking, wait for the answer."""
        if self._already_done(request.action, task_id):
            return Decision(Verdict.REFUSE, _ALREADY_DONE)
        if (said_no := self.refused_paths(request.action.named_paths(), task_id)) is not None:
            return Decision(Verdict.REFUSE, said_no)
        if self._take_grant(request, task_id):
            return Decision(Verdict.ALLOW, "decided with the other calls on the card that named it")
        if covered := self._approved_in_task(request, task_id):
            ruling = Ruling(Verdict.ALLOW, f"you approved {covered} earlier in this task", DecidedBy.REPLAY)
            self._record(request, ruling, task_id)
            return Decision(ruling.verdict, ruling.rule)
        ruling = await self._policy.rule(request.action)
        if request.requires_confirmation and ruling.verdict is not Verdict.REFUSE:
            return await self._ask(request, task_id)
        if ruling.verdict is Verdict.ASK:
            return await self._ask(request, task_id)
        if _worth_recording(request.action):
            self._record(request, ruling, task_id)
        return Decision(ruling.verdict, ruling.rule)

    async def decide_ahead(
        self, requests: Sequence[Request], *, task_id: str | None, title: str, message: str
    ) -> Decision:
        """Decide now, on one card at most, the calls an agent will make for one thing; each then runs once.

        An agent asked on a card of its own and then the tool asked again: an approved "Delete
        old_safe.log" was followed by bash asking, and one delete took three cards (issue #76, A14).
        Each call is ruled on as `decide` would rule on it; those the policy would ask about share one
        card, and a yes (or the policy's allow) lets each named call run once without asking again.
        """
        if not task_id:
            return Decision(Verdict.REFUSE, "a call can be decided ahead only within a task")
        for request in requests:
            if self._already_done(request.action, task_id):
                return Decision(Verdict.REFUSE, _ALREADY_DONE)
            if (said_no := self.refused_paths(request.action.named_paths(), task_id)) is not None:
                return Decision(Verdict.REFUSE, said_no)
        rulings = [await self._policy.rule(request.action) for request in requests]
        if refused := next((ruling for ruling in rulings if ruling.verdict is Verdict.REFUSE), None):
            return Decision(Verdict.REFUSE, refused.rule)
        asking = [
            r
            for r, ruling in zip(requests, rulings, strict=True)
            if r.requires_confirmation or ruling.verdict is Verdict.ASK
        ]
        if asking:
            decision = await self._ask_together(asking, task_id, title, message)
            if not decision.allowed:
                return decision
        for request, ruling in zip(requests, rulings, strict=True):
            if request not in asking and _worth_recording(request.action):
                self._record(request, ruling, task_id)
        self._granted.setdefault(task_id, Counter()).update(request.action.describe() for request in requests)
        if asking:
            return decision
        deciders = {ruling.decided_by for ruling in rulings}
        decided_by = deciders.pop() if len(deciders) == 1 else DecidedBy.NORTH
        return Decision(Verdict.ALLOW, "; ".join(sorted({r.rule for r in rulings})), decided_by=decided_by)

    async def _ask_together(self, requests: Sequence[Request], task_id: str, title: str, message: str) -> Decision:
        """One card for *requests*: what the agent wants and why, then each call it will make."""
        if self._interaction is None:
            return Decision(Verdict.REFUSE, _NOBODY_TO_ASK)
        calls = "\n".join(f"- `{request.action.summary}`" for request in requests)
        card = Card.new(
            type=CardType.APPROVAL,
            task_id=task_id,
            agent=requests[0].action.agent,
            title=title,
            message=f"{message}\n\nApproving lets north do exactly this, without asking again:\n{calls}",
            options=list(APPROVAL_DEFAULT_OPTIONS),
            action_key="\n".join(request.action.describe() for request in requests),
        )
        card = await self._interaction.ask_person(card, event=CardEvent.APPROVAL)
        if card.status == ApprovalDecision.REJECTED and self._rejections is not None:
            for request in requests:
                self._rejections.record(task_id, request.action.named_paths(), request.action.summary)
        verdict = Verdict.ALLOW if card.status == ApprovalDecision.APPROVED else Verdict.REFUSE
        return Decision(verdict, "you decided", status=card.status, decided_by=card.decided_by or DecidedBy.YOU)

    def _take_grant(self, request: Request, task_id: str | None) -> bool:
        """Whether *request* is a call decided ahead in this task; using it spends it."""
        granted = self._granted.get(task_id or "")
        key = request.action.describe()
        if not granted or granted[key] <= 0:
            return False
        granted[key] -= 1
        return True

    def refused_paths(self, paths: Iterable[Path], task_id: str | None) -> str | None:
        """Why touching *paths* is refused in this task - a person said no to an action naming them - or None.

        Reads ask this too: a "no" that a read could route around was no "no".
        """
        if self._rejections is None or not task_id:
            return None
        for path in paths:
            if (covering := self._rejections.covering(task_id, path)) is not None:
                rejected, summary = covering
                return (
                    f"you said no to `{summary}` earlier in this task, and that covers {rejected}. "
                    "Do not get it another way: tell the user what you needed it for and ask how to go on"
                )
        return None

    def _approved_in_task(self, request: Request, task_id: str | None) -> str:
        """What a person approved earlier in this task that covers this request, or "".

        The exact action, or its scope: the yes to opening a site in a profile covers
        reading that site there for the rest of the task. That memory was the browser's
        own and failed, so every page asked again (issue #76, W7).
        """
        if not task_id or request.requires_confirmation or not may_replay_yes(request.action):
            return ""
        approved = self._approved.get(task_id, set())
        if request.action.describe() in approved:
            return "this exact action"
        return f"this: {request.action.scope}" if request.action.scope in approved else ""

    def completed(self, request: Request, task_id: str | None) -> None:
        """Note that *request* ran successfully, so an irreversible one is never repeated in this task."""
        if self._effects is None or not task_id or not forbidden_reason(request.action):
            return
        self._effects.record(task_id, request.action.describe(), request.action.summary)

    def _already_done(self, action: Action, task_id: str | None) -> bool:
        if self._effects is None or not task_id or not forbidden_reason(action):
            return False
        return self._effects.done(task_id, action.describe())

    async def _ask(self, request: Request, task_id: str | None) -> Decision:
        if self._interaction is None:
            return Decision(Verdict.REFUSE, _NOBODY_TO_ASK)
        card = await self._interaction.ask_person(self._card(request, task_id), event=CardEvent.APPROVAL)
        verdict = Verdict.ALLOW if card.status == ApprovalDecision.APPROVED else Verdict.REFUSE
        if verdict is Verdict.ALLOW and task_id:
            covers = (request.action.describe(), request.action.scope)
            self._approved.setdefault(task_id, set()).update(key for key in covers if key)
        if card.status == ApprovalDecision.REJECTED and task_id and self._rejections is not None:
            self._rejections.record(task_id, request.action.named_paths(), request.action.summary)
        return Decision(verdict, "you decided", status=card.status)

    def _record(self, request: Request, ruling: Ruling, task_id: str | None) -> None:
        """Leave a resolved card, with its reason, for an action decided without asking.

        An auto-approval nobody can see is indistinguishable from one that never
        happened, and that invisibility is most of why raising autonomy felt unsafe.
        A refusal is recorded too: it is as much a decision taken for you.
        """
        if self._interaction is None:
            return
        if ruling.allowed:
            decision, chosen = ApprovalDecision.APPROVED, approve_option(list(request.options))
        else:
            decision, chosen = ApprovalDecision.REJECTED, ""
        answer = Answer(decision, chosen, ruling.rule, ruling.decided_by, ruling.memory_used)
        self._interaction.record_resolved(self._card(request, task_id), answer)

    @staticmethod
    def _card(request: Request, task_id: str | None) -> Card:
        return Card.new(
            type=CardType.APPROVAL,
            task_id=task_id,
            agent=request.action.agent,
            title=request.title,
            message=request.message,
            options=list(request.options),
            action_key=request.action.describe(),
        )


def _worth_recording(action: Action) -> bool:
    """A read-only action was never a decision; anything that changes something is."""
    return action.mutating and not action.read_only
