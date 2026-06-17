"""Judgement Rules Filter - pre-screens cards against learned rules.

Before any card reaches the Notifier, this filter reads judgement_rules.md
and asks a fast LLM whether an existing rule clearly covers the situation.
Confidence >= 0.8 triggers an automatic decision (approved / rejected /
answered); anything below surfaces the card to the user as normal.

High-stakes APPROVAL cards from specific agents are never auto-resolved
regardless of confidence - the user always gets to see them.

See README Sections 9.4 and 9.5.
"""

from __future__ import annotations

import json
import logging

from approval.models import Card, CardType
from context.base import ContextStore
from context.models import ContextDocument
from inference.base import InferenceRouter
from inference.models import CompletionRequest, PoolPriority

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

_PROMPT_TEMPLATE = """\
You are the Judgement Rules Filter for a personal AI operating system called north.

The user has a set of learned decision rules in judgement_rules.md:

---
{rules}
---
{preferences}
A card is about to be surfaced to the user:
  Type:    {card_type}
  Agent:   {agent}
  Title:   {title}
  Message: {message}
  Options: {options}

Does a learned rule (or, for a QUESTION, a known preference) clearly determine the
outcome - so the user does not need to be asked again?

Reply with JSON only - no prose:
{{
  "decision": "approved" | "rejected" | "answered" | "none",
  "chosen_option": "<the answer text if answered, else empty string>",
  "confidence": <0.0 to 1.0>,
  "rule": "<one-line summary of the matching rule/preference, or empty string>"
}}

Rules:
- Use "none" if nothing clearly applies, or confidence is below {threshold}.
- Use "approved" only for APPROVAL cards where a rule clearly says to approve.
- Use "rejected" only for APPROVAL cards where a rule clearly says to reject.
- Use "answered" only for QUESTION cards where a learned rule or a known preference
  clearly determines the answer. Put that answer in chosen_option - it need not be
  one of the listed options.
- INFORMATION cards should always return "none" (they need no decision).
- When in doubt, return "none" - asking the user is always safer than guessing.
"""


class JudgementFilter:
    """Checks a Card against judgement_rules.md before it reaches the Notifier.

    Returns (decision, chosen_option) if a rule fires at high confidence,
    or (None, "") to mean "surface to user as normal".
    """

    def __init__(
        self,
        context_store: ContextStore,
        inference_router: InferenceRouter,
    ) -> None:
        self._context_store = context_store
        self._inference_router = inference_router

    async def check(self, card: Card) -> tuple[str | None, str]:
        """Return (decision, chosen_option) or (None, '') if nothing fires.

        APPROVAL cards are judged against learned rules only. QUESTION cards also
        consider the user's known preferences (public.md), so a preference stated
        once - by answering an earlier question - can answer the next one without
        re-asking.
        """
        # INFORMATION cards never need filtering - they carry no decision.
        if card.type == CardType.INFORMATION:
            return None, ""

        rules = await self._context_store.read(ContextDocument.JUDGEMENT_RULES) or ""
        preferences = ""
        if card.type == CardType.QUESTION:
            preferences = await self._context_store.read(ContextDocument.PUBLIC) or ""

        # Need at least some learned context to act on, or there is nothing to match.
        if len((rules + preferences).strip()) < _MIN_LEARNED_CONTEXT_CHARS:
            return None, ""

        prompt = _PROMPT_TEMPLATE.format(
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
            result = json.loads(response.text.strip())
        except Exception:
            logger.debug("JudgementFilter: LLM call failed, surfacing card %s", card.id)
            return None, ""

        decision = result.get("decision", "none")
        confidence = float(result.get("confidence", 0.0))
        chosen_option = str(result.get("chosen_option", ""))
        rule = result.get("rule", "")

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
