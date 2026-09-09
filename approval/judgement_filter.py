"""Judgement Rules Filter - what the user's learned rules say about a card.

Reads judgement_rules.md and asks a fast LLM whether an existing rule clearly
covers the situation. Confidence >= 0.8 produces an answer; anything below
abstains and the card goes to the user as normal.

This is *one tier of* the approval decision, not the decision. For an action,
``ApprovalPolicy`` calls ``advise`` last and only in ``auto`` and above - this
module no longer reads the approval mode, replays learned decisions, or knows
about the safe subset. It used to do all three, in a copy that had drifted from
the tools' copies, and its own tier had no mode check at all.

``check`` still answers QUESTION cards directly, because a question is not an
action and the policy has nothing to say about it.

High-stakes agents are never auto-approved here regardless of confidence.

See README Sections 9.4 and 9.5.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from approval.approval_memory import ApprovalMemory
from approval.interaction import APPROVAL_DEFAULT_OPTIONS
from approval.mode import ApprovalMode
from approval.models import Card, CardType
from approval.policy import Action
from inference.base import InferenceRouter
from inference.models import CompletionRequest, PoolPriority
from memory import ContextDocument, MemoryGateway
from utils.prompts import load_prompt
from utils.text import extract_json

logger = logging.getLogger(__name__)

_AUTO_CONFIDENCE_THRESHOLD = 0.8
# Below this much learned text (rules + preferences) there is nothing to match on.
_MIN_LEARNED_CONTEXT_CHARS = 20

# APPROVAL cards from these sources gate mutating/destructive actions (shell
# commands, file patches, git/gh writes, runtime tool changes, device control).
# They are NEVER auto-approved - a human must see them - regardless of what the
# rules or the LLM say. Auto-rejection stays allowed (rejecting is safe).
NEVER_AUTO_APPROVE_AGENTS: frozenset[str] = frozenset(
    {"bash", "shell", "patch_file", "create_tool", "git", "gh", "kasa"}
)


class JudgementFilter:
    """Checks a Card against judgement_rules.md before it reaches the Notifier.

    Returns (decision, chosen_option) if a rule fires at high confidence,
    or (None, "") to mean "surface to user as normal".
    """

    def __init__(
        self,
        memory: MemoryGateway,
        inference_router: InferenceRouter,
        approval_memory: ApprovalMemory | None = None,
        mode_provider: Callable[[], ApprovalMode] | None = None,
    ) -> None:
        self._memory = memory
        self._inference_router = inference_router
        self._approval_memory = approval_memory
        self._mode_provider = mode_provider
        self._last_rule = ""

    def _mode(self) -> ApprovalMode:
        return self._mode_provider() if self._mode_provider is not None else ApprovalMode.INTERACTIVE

    async def advise(self, action: Action) -> tuple[str | None, str]:
        """The learned judgement rules' opinion on *action*, for `ApprovalPolicy`.

        One tier of the decision, not the decision. The policy calls this last
        and only in `auto` and above; it used to be reached in every mode, which
        is how a model came to approve actions in `interactive`.

        Returns ("approved"|"rejected", rule) or (None, "") to abstain.
        """
        card = Card(
            id="",
            type=CardType.APPROVAL,
            task_id="",
            agent=action.agent,
            title=action.summary,
            message=action.describe(),
            options=list(APPROVAL_DEFAULT_OPTIONS),
        )
        decision, _ = await self.check(card)
        return decision, self._last_rule

    async def check(self, card: Card) -> tuple[str | None, str]:
        """Return (decision, chosen_option) or (None, '') if nothing fires.

        Consults only the learned judgement rules, via a model. The approval
        mode, the deterministic safe subset, and the user's replayed decisions
        all live in `approval/policy.py` now - keeping a second copy here is how
        the two drifted apart, one of them without a mode check.
        """
        # INFORMATION cards never need filtering - they carry no decision.
        if card.type == CardType.INFORMATION:
            return None, ""

        rules = await self._memory.read_document(ContextDocument.JUDGEMENT_RULES)
        preferences = ""
        if card.type == CardType.QUESTION:
            preferences = await self._memory.read_document(ContextDocument.USER)

        # Need at least some learned context to act on, or there is nothing to match.
        if len((rules + preferences).strip()) < _MIN_LEARNED_CONTEXT_CHARS:
            return None, ""

        prompt = load_prompt("prompts/judgement_filter.md").format(
            rules=rules[:3000] or "(no rules learned yet)",
            preferences=(
                f"\nThe user's known preferences and identity:\n---\n{preferences[:2000]}\n---\n"
                if preferences.strip()
                else ""
            ),
            card_type=card.type.value,
            agent=card.agent,
            title=card.title,
            message=card.message[:500],
            options=", ".join(card.options) if card.options else "none",
            threshold=_AUTO_CONFIDENCE_THRESHOLD,
        )

        try:
            response = await self._inference_router.complete(
                CompletionRequest(
                    prompt=prompt,
                    priority=PoolPriority.MEDIUM,
                    component="judgement_filter",
                    task_id=card.task_id,
                    json_mode=True,
                )
            )
            result = extract_json(response.text)
        except Exception:
            logger.debug("JudgementFilter: LLM call failed, surfacing card %s", card.id)
            return None, ""

        decision = result.get("decision", "none")
        confidence = float(result.get("confidence", 0.0))
        chosen_option = str(result.get("chosen_option", ""))
        rule = result.get("rule", "")
        self._last_rule = rule

        if decision == "none" or confidence < _AUTO_CONFIDENCE_THRESHOLD:
            return None, ""

        # Fail-closed gate: destructive tool classes always require a human for
        # approval. This check is here - in the single producer of auto-decisions
        # - so every caller (BashTool, ShellTool, PatchFileTool, CreateToolTool,
        # GitTool, GhTool, agents, the orchestrator) inherits it.
        if decision == "approved" and card.type == CardType.APPROVAL and card.agent in NEVER_AUTO_APPROVE_AGENTS:
            logger.info(
                "JudgementFilter: refusing to auto-approve high-stakes card %s from %r - surfacing to user",
                card.id,
                card.agent,
            )
            return None, ""

        logger.info(
            "JudgementFilter: auto-%s card %s (confidence=%.2f, rule=%r)",
            decision,
            card.id,
            confidence,
            rule,
        )
        return decision, chosen_option
