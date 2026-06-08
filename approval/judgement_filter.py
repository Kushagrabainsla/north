"""Judgement Rules Filter — pre-screens cards against learned rules.

Before any card reaches the Notifier, this filter reads judgement_rules.md
and asks a fast LLM whether an existing rule clearly covers the situation.
Confidence >= 0.8 triggers an automatic decision (approved / rejected /
answered); anything below surfaces the card to the user as normal.

High-stakes APPROVAL cards from specific agents are never auto-resolved
regardless of confidence — the user always gets to see them.

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

_PROMPT_TEMPLATE = """\
You are the Judgement Rules Filter for a personal AI operating system called north.

The user has a set of learned decision rules in judgement_rules.md:

---
{rules}
---

A card is about to be surfaced to the user:
  Type:    {card_type}
  Agent:   {agent}
  Title:   {title}
  Message: {message}
  Options: {options}

Does any rule clearly cover this situation and indicate an automatic decision?

Reply with JSON only — no prose:
{{
  "decision": "approved" | "rejected" | "answered" | "none",
  "chosen_option": "<option text if answered, else empty string>",
  "confidence": <0.0 to 1.0>,
  "rule": "<one-line summary of the matching rule, or empty string>"
}}

Rules:
- Use "none" if no rule clearly applies, or confidence is below {threshold}.
- Use "approved" only for APPROVAL cards where the rule clearly says to approve.
- Use "rejected" only for APPROVAL cards where the rule clearly says to reject.
- Use "answered" only for QUESTION cards where a rule pre-selects an option.
- INFORMATION cards should always return "none" (they need no decision).
- When in doubt, return "none" — surfacing a card is always safer than suppressing it.
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
        """Return (decision, chosen_option) or (None, '') if no rule fires."""
        # INFORMATION cards never need filtering — they carry no decision.
        if card.type == CardType.INFORMATION:
            return None, ""

        rules = await self._context_store.read(ContextDocument.JUDGEMENT_RULES)
        if not rules or len(rules.strip()) < 20:
            return None, ""

        prompt = _PROMPT_TEMPLATE.format(
            rules=rules[:3000],
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

        logger.info(
            "JudgementFilter: auto-%s card %s (confidence=%.2f, rule=%r)",
            decision,
            card.id,
            confidence,
            rule,
        )
        return decision, chosen_option
