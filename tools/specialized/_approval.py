"""The one way a tool asks whether it may act.

Every gated tool - bash, shell, git, gh, patch_file, create_tool - describes
what it is about to do as an `Action` and hands it to `gate_action`. That is
the whole of the tool's involvement: it does not read the approval mode, does
not hold an allowlist, and does not decide anything. `approval/policy.py` rules
on the action; this module turns that ruling into either "carry on" or the
`ToolOutput` the tool must return instead.

Before this, each tool carried its own copy of the decision and they had drifted
apart - one had a mode check the others lacked, and the model-backed tier had
none at all, so it auto-approved in the default mode. One seam is the point.

Fail-closed everywhere: a tool wired without a policy refuses to act rather
than acting unguarded, because a missing gate must never read as an open one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from approval.interaction import APPROVAL_DEFAULT_OPTIONS, CardEvent, UserInteraction
from approval.models import ApprovalDecision, Card, CardType
from approval.policy import Action, ApprovalPolicy, Verdict
from tools.models import ToolOutput

if TYPE_CHECKING:
    from approval.base import Notifier
    from approval.policy import Ruling
    from approval.store import ApprovalStore
    from orchestrator.stream import EventStreamManager

_DEFAULT_OPTIONS = ("Run", "Cancel")

# Used when a tool was constructed without one (an auto-discovered instance).
# Interactive, with nothing learned and no allowlist - so it allows read-only
# work and asks about everything else.
_DEFAULT_POLICY = ApprovalPolicy()


async def gate_action(
    action: Action,
    *,
    policy: ApprovalPolicy | None,
    approval_store: ApprovalStore | None,
    title: str,
    message: str,
    task_id: str | None = None,
    options: tuple[str, ...] = _DEFAULT_OPTIONS,
    stream_manager: EventStreamManager | None = None,
    notifier: Notifier | None = None,
    timeout: float = 300.0,
    declined: str = "Action cancelled by user.",
    refused_hint: str = "",
) -> ToolOutput | None:
    """``None`` when the tool may proceed; otherwise what it must return instead.

    An allowed action still leaves a resolved card behind, unless it was merely
    read-only. Something north did without asking has to be visible afterwards -
    an auto-approval nobody can see is indistinguishable from one that never
    happened, and that invisibility is most of why raising autonomy felt unsafe.
    """
    # An un-injected tool still gets a real policy, just an empty one: no
    # allowlist, no learned decisions, interactive. Read-only work then passes
    # on the policy's own first tier rather than on a second copy of it here,
    # and anything mutating falls through to the store check below - which
    # refuses when there is nobody to ask. Fail-closed without special-casing.
    ruling = await (policy or _DEFAULT_POLICY).rule(action)

    if ruling.verdict is Verdict.REFUSE:
        # Not a rejection by a person - nobody was asked. Saying "cancelled by
        # user" here would send the agent looking for the human who declined.
        # *Why* it is refused is the policy's; what to do instead is the tool's.
        blocked = f"Blocked: {ruling.rule}. This was refused outright and never shown to the user."
        return ToolOutput(success=False, failure_kind="refused", error=f"{blocked} {refused_hint}".strip())

    if ruling.verdict is Verdict.ALLOW:
        if _worth_recording(action):
            _record_auto_decision(approval_store, action, ruling, title, message, task_id)
        return None

    if approval_store is None:
        return _ungated(action.agent)

    interaction = UserInteraction(
        approval_store,
        notifier=notifier,
        stream_manager=stream_manager,
        default_timeout=timeout,
    )
    card = await interaction.request_decision(
        Card.new(
            type=CardType.APPROVAL,
            task_id=task_id,
            agent=action.agent,
            title=title,
            message=message,
            options=list(options or APPROVAL_DEFAULT_OPTIONS),
        ),
        event=CardEvent.APPROVAL,
        timeout=timeout,
    )
    return refusal_output(card.status, timeout=timeout, declined=declined)


def _worth_recording(action: Action) -> bool:
    """Whether an allowed action is one the user would want to see afterwards.

    A read-only action was never a decision, so recording every ``ls`` would
    bury the ones that mattered. Anything that changes something is recorded.
    """
    return action.mutating and not action.read_only


def _record_auto_decision(
    approval_store: ApprovalStore | None,
    action: Action,
    ruling: Ruling,
    title: str,
    message: str,
    task_id: str | None,
) -> None:
    """Leave an already-resolved card for an action that ran without asking."""
    if approval_store is None:
        return
    card = Card.new(
        type=CardType.APPROVAL,
        task_id=task_id,
        agent=action.agent,
        title=title,
        message=message,
        options=list(APPROVAL_DEFAULT_OPTIONS),
    )
    approval_store.add(card)
    approval_store.resolve(card.id, ApprovalDecision.APPROVED, chosen_option=ruling.rule)


def _ungated(agent: str) -> ToolOutput:
    """A tool wired without a gate refuses; a missing gate is not an open gate."""
    return ToolOutput(
        success=False,
        failure_kind="refused",
        error=(
            f"{agent}: this action changes state and requires approval, but no approval policy "
            "is configured for this tool instance. Refusing (fail closed)."
        ),
    )


def refusal_output(status: str, *, timeout: float, declined: str) -> ToolOutput | None:
    """``None`` when approved; otherwise the ToolOutput the tool must return.

    The single place that turns an approval outcome into a tool result, so every
    gated tool tells the agent the same two things: that a refusal is not the
    tool malfunctioning (``failure_kind="refused"``, which keeps an absent human
    from being counted against the tool), and that an expired card is nobody
    saying no rather than someone saying no.
    """
    if status == ApprovalDecision.APPROVED:
        return None
    if status == ApprovalDecision.TIMEOUT_REJECTED:
        return ToolOutput(
            success=False,
            failure_kind="refused",
            data={"unanswered": True},
            error=(
                f"No one answered the approval request within {timeout:.0f}s, so the action did "
                "not run. Nobody rejected it - there is simply no one available to approve. "
                "Do not retry this or look for another way to do it."
            ),
        )
    return ToolOutput(success=False, failure_kind="refused", error=declined)
