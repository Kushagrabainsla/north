"""Shared command/diff approval flow for specialized tools.

BashTool, ShellTool, and PatchFileTool all gate an action behind the same
approval card: optionally consult the learned JudgementFilter, otherwise surface
a card to the user and wait for a decision. This is the single definition of
that flow so the three tools never drift (see CODING_STYLE §5 DRY).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from approval.models import ApprovalDecision, Card, CardType
from utils.ids import generate_id

if TYPE_CHECKING:
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
    timeout: float = 300.0,
) -> bool:
    """Return True when the action is approved (by a learned rule or the user).

    Checks the JudgementFilter first; if it does not decide, surfaces a card and
    waits up to *timeout* seconds. A timeout is treated as a rejection.
    """
    card = Card(
        id=generate_id(),
        type=CardType.APPROVAL,
        task_id=task_id or "",
        agent=agent,
        title=title,
        message=message,
        options=list(options),
    )

    if judgement_filter is not None:
        try:
            auto_decision, _ = await judgement_filter.check(card)
            if auto_decision == ApprovalDecision.APPROVED:
                return True
            if auto_decision == ApprovalDecision.REJECTED:
                return False
        except Exception:
            pass

    approval_store.add(card)
    if stream_manager and task_id:
        await stream_manager.emit(
            task_id,
            "approval_required",
            {
                "card_id": card.id,
                "task_id": task_id,
                "agent": agent,
                "title": title,
                "message": message,
                "options": card.options,
            },
        )

    resolved = await approval_store.wait_for_decision(card.id, timeout=timeout)
    if resolved is None:
        approval_store.resolve(card.id, ApprovalDecision.REJECTED)
        return False
    return resolved.chosen_option.lower() == card.options[0].lower()
