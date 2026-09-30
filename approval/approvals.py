"""The approval layer's one entry point: may north do this?

Every tool call that changes something reaches this class through
`Tool.execute`, so no tool can forget to ask. Before it existed each gated tool
called the gate itself, and twelve mutating tools - the browser, `north_config`
(which could switch the approval mode), schedules, MCP tools - simply never did.

The split is unchanged: the caller describes the action as facts (`Request`),
`ApprovalPolicy` rules on it, and only an ``ASK`` becomes a card for the user.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from approval.interaction import APPROVAL_DEFAULT_OPTIONS, CardEvent, UserInteraction
from approval.models import ApprovalDecision, Card, CardType
from approval.policy import Action, Answer, ApprovalPolicy, Verdict
from config.approval_mode import approve_option

if TYPE_CHECKING:
    from approval.policy import Ruling

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


@dataclass(frozen=True)
class Decision:
    """How a request was decided.

    ``status`` is set only when a card was raised - it tells "a person said no"
    apart from "nobody answered". A policy refusal has no status: nobody was asked.
    """

    verdict: Verdict
    reason: str
    status: ApprovalDecision | None = None

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW


class Approvals:
    """Rules on a `Request`; asks the user only when the policy says ASK."""

    def __init__(self, policy: ApprovalPolicy, interaction: UserInteraction | None) -> None:
        self._policy = policy
        self._interaction = interaction

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
        ruling = await self._policy.rule(request.action)
        if ruling.verdict is Verdict.ASK:
            return await self._ask(request, task_id)
        if _worth_recording(request.action):
            self._record(request, ruling, task_id)
        return Decision(ruling.verdict, ruling.rule)

    async def _ask(self, request: Request, task_id: str | None) -> Decision:
        if self._interaction is None:
            return Decision(Verdict.REFUSE, _NOBODY_TO_ASK)
        card = await self._interaction.ask_person(self._card(request, task_id), event=CardEvent.APPROVAL)
        verdict = Verdict.ALLOW if card.status == ApprovalDecision.APPROVED else Verdict.REFUSE
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
        answer = Answer(decision, chosen, ruling.rule, ruling.memory_used)
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
