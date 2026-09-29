"""Judgement Rules Filter - what the user's learned rules say about a card.

Reads judgement_rules.md and asks a fast LLM whether an existing rule clearly
covers the situation. Confidence >= 0.8 produces an answer; anything below
abstains and the card goes to the user as normal.

This is one input to the approval decision, not the decision. ``ApprovalPolicy``
consults ``check`` for a QUESTION card only in the modes that answer for you -
it does not read the approval mode, replay learned decisions, or know about the
safe subset. It used to do all three, and its model tier answered questions even
in the strictest mode.

High-stakes agents are never auto-approved here regardless of confidence.

See README Sections 9.4 and 9.5.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from approval.models import Card, CardType
from inference.base import InferenceRouter
from inference.models import CompletionRequest, PoolPriority
from memory import ContextDocument, MemoryGateway
from utils.prompts import load_prompt
from utils.text import extract_json

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Verdict:
    """What the learned rules said. Abstaining is the default."""

    decision: str | None = None
    chosen_option: str = ""
    rule: str = ""


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

    def __init__(self, memory: MemoryGateway, inference_router: InferenceRouter) -> None:
        self._memory = memory
        self._inference_router = inference_router

    async def check(self, card: Card) -> tuple[str | None, str]:
        """Return (decision, chosen_option) or (None, '') if nothing fires.

        The policy's question advisor (`ApprovalPolicy.answer`).
        """
        # INFORMATION cards never need filtering - they carry no decision.
        if card.type == CardType.INFORMATION:
            return None, ""
        verdict = await self._consult(
            card_type=card.type,
            agent=card.agent,
            title=card.title,
            body=card.message,
            options=card.options,
            task_id=card.task_id,
        )
        return verdict.decision, verdict.chosen_option

    async def _consult(
        self,
        *,
        card_type: CardType,
        agent: str,
        title: str,
        body: str,
        options: list[str],
        task_id: str = "",
    ) -> _Verdict:
        """Ask the learned rules about one thing. The only place that does."""
        rules = await self._memory.read_document(ContextDocument.JUDGEMENT_RULES)
        preferences = ""
        if card_type == CardType.QUESTION:
            preferences = await self._memory.read_document(ContextDocument.USER)

        # Need at least some learned context to act on, or there is nothing to match.
        if len((rules + preferences).strip()) < _MIN_LEARNED_CONTEXT_CHARS:
            return _Verdict()

        prompt = load_prompt("prompts/judgement_filter.md").format(
            rules=rules[:3000] or "(no rules learned yet)",
            preferences=(
                f"\nThe user's known preferences and identity:\n---\n{preferences[:2000]}\n---\n"
                if preferences.strip()
                else ""
            ),
            card_type=card_type.value,
            agent=agent,
            title=title,
            message=body[:500],
            options=", ".join(options) if options else "none",
            threshold=_AUTO_CONFIDENCE_THRESHOLD,
        )

        try:
            response = await self._inference_router.complete(
                CompletionRequest(
                    prompt=prompt,
                    priority=PoolPriority.MEDIUM,
                    component="judgement_filter",
                    task_id=task_id,
                    json_mode=True,
                )
            )
            result = extract_json(response.text)
        except Exception:
            logger.debug("JudgementFilter: LLM call failed - surfacing to the user")
            return _Verdict()

        decision = result.get("decision", "none")
        confidence = float(result.get("confidence", 0.0))
        chosen_option = str(result.get("chosen_option", ""))
        rule = result.get("rule", "")

        if decision == "none" or confidence < _AUTO_CONFIDENCE_THRESHOLD:
            return _Verdict()

        # Fail-closed gate: destructive tool classes always require a human for
        # approval. This check is here - in the single producer of auto-decisions
        # - so every caller (BashTool, ShellTool, PatchFileTool, CreateToolTool,
        # GitTool, GhTool, agents, the orchestrator) inherits it.
        if decision == "approved" and card_type == CardType.APPROVAL and agent in NEVER_AUTO_APPROVE_AGENTS:
            logger.info(
                "JudgementFilter: refusing to auto-approve a high-stakes action from %r - surfacing to user",
                agent,
            )
            return _Verdict()

        logger.info(
            "JudgementFilter: auto-%s an action from %r (confidence=%.2f, rule=%r)",
            decision,
            agent,
            confidence,
            rule,
        )
        return _Verdict(decision=decision, chosen_option=chosen_option, rule=rule)
