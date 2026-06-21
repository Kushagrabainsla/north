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
    *timeout* seconds. A timeout is treated as a rejection.
    """
    interaction = UserInteraction(
        approval_store,
        notifier=notifier,
        judgement_filter=judgement_filter,
        stream_manager=stream_manager,
        default_timeout=timeout,
    )
    return await interaction.request_approval(
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
) -> str | None:
    """Fail-closed approval gate for mutating tool actions.

    Returns ``None`` when the action may proceed, or an error string the tool
    must return as a failure. Without an ApprovalStore (e.g. an auto-discovered
    instance that never got its dependencies injected) the action is refused  - 
    a missing gate must never mean an open gate.
    """
    if approval_store is None:
        return (
            f"{agent}: this action mutates state and requires user approval, but no approval "
            "gate is configured for this tool instance. Refusing (fail closed)."
        )
    approved = await request_approval_decision(
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
    return None if approved else "Action rejected by user."
