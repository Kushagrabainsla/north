"""Shared command/diff approval flow for specialized tools.

BashTool, ShellTool, PatchFileTool, CreateToolTool, GitTool, GhTool, and
KasaTool all gate an action behind the same approval card: optionally consult
the learned JudgementFilter, otherwise surface a card to the user and wait for
a decision. ``gate_mutating_action`` is the fail-closed wrapper for tools whose
approval dependencies are optional: without a wired ApprovalStore, mutating
actions are refused - never silently allowed. This is the single definition of
that flow so the tools never drift (see CODING_STYLE §5 DRY).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from approval.interaction import UserInteraction
from approval.models import ApprovalDecision
from tools.models import ToolOutput

if TYPE_CHECKING:
    from approval.base import Notifier
    from approval.judgement_filter import JudgementFilter
    from approval.store import ApprovalStore
    from orchestrator.stream import EventStreamManager

_DEFAULT_OPTIONS = ("Run", "Cancel")


async def request_approval_decision(
    approval_store: ApprovalStore,
    *,
    task_id: str | None,
    agent: str,
    title: str,
    message: str,
    options: tuple[str, str] = _DEFAULT_OPTIONS,
    stream_manager: EventStreamManager | None = None,
    judgement_filter: JudgementFilter | None = None,
    notifier: Notifier | None = None,
    timeout: float = 300.0,
) -> bool:
    """Return True when the action is approved (by a learned rule or the user).

    Thin tool-facing adapter over the shared ``UserInteraction`` mediator: it
    consults the JudgementFilter, surfaces an APPROVAL card, and blocks up to
    *timeout* seconds. A timeout is treated as a rejection - callers that need
    to tell those apart should use ``request_approval_status``.
    """
    status = await request_approval_status(
        approval_store,
        task_id=task_id,
        agent=agent,
        title=title,
        message=message,
        options=options,
        stream_manager=stream_manager,
        judgement_filter=judgement_filter,
        notifier=notifier,
        timeout=timeout,
    )
    return status == ApprovalDecision.APPROVED


async def request_approval_status(
    approval_store: ApprovalStore,
    *,
    task_id: str | None,
    agent: str,
    title: str,
    message: str,
    options: tuple[str, str] = _DEFAULT_OPTIONS,
    stream_manager: EventStreamManager | None = None,
    judgement_filter: JudgementFilter | None = None,
    notifier: Notifier | None = None,
    timeout: float = 300.0,
) -> ApprovalDecision:
    """Same flow as ``request_approval_decision``, but reporting how it resolved."""
    interaction = UserInteraction(
        approval_store,
        notifier=notifier,
        judgement_filter=judgement_filter,
        stream_manager=stream_manager,
        default_timeout=timeout,
    )
    return await interaction.request_approval_status(
        task_id=task_id,
        agent=agent,
        title=title,
        message=message,
        options=options,
        timeout=timeout,
    )


async def gate_mutating_action(
    approval_store: ApprovalStore | None,
    *,
    agent: str,
    title: str,
    message: str,
    task_id: str | None,
    stream_manager: EventStreamManager | None = None,
    judgement_filter: JudgementFilter | None = None,
    notifier: Notifier | None = None,
    timeout: float = 300.0,
) -> ToolOutput | None:
    """Fail-closed approval gate for mutating tool actions.

    Returns ``None`` when the action may proceed, or the ``ToolOutput`` the tool
    must return instead. Without an ApprovalStore (e.g. an auto-discovered
    instance that never got its dependencies injected) the action is refused  -
    a missing gate must never mean an open gate.

    An unanswered card is reported as unanswered, never as a rejection. Saying
    "rejected by user" when nobody was watching sent agents looking for another
    way to do the same thing, and each attempt stalled for the full timeout.
    """
    if approval_store is None:
        return ToolOutput(
            success=False,
            failure_kind="refused",
            error=(
                f"{agent}: this action mutates state and requires user approval, but no approval "
                "gate is configured for this tool instance. Refusing (fail closed)."
            ),
        )
    status = await request_approval_status(
        approval_store,
        task_id=task_id,
        agent=agent,
        title=title,
        message=message,
        options=("Approve", "Reject"),
        stream_manager=stream_manager,
        judgement_filter=judgement_filter,
        notifier=notifier,
        timeout=timeout,
    )
    return refusal_output(status, timeout=timeout, declined="Action rejected by user.")


def refusal_output(status: ApprovalDecision, *, timeout: float, declined: str) -> ToolOutput | None:
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
