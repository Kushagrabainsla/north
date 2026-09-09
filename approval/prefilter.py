"""Dropping a candidate you would obviously reject, before it costs you a look.

Your queue should get shorter every week instead of staying the same size. The
material for that already exists once decisions are logged: items you rejected,
and items you approved.

## Why the threshold is relative

An absolute cosine cutoff does not work here, and north has learned this before
in `memory/embeddings.py`: similarity scores are only meaningful against the
spread of the particular corpus. One flow's candidates are all near-identical
job postings and score 0.9 against everything; another's are varied and top out
at 0.4. A fixed cutoff filters everything for the first and nothing for the
second.

So the comparison is between two things measured the same way: how close is this
candidate to what you rejected, versus to what you approved? A candidate is
dropped only when it is *clearly* nearer the rejections - a margin, not a score.
That is scale-free, and it also means a flow with no approvals yet filters
nothing, which is the correct behaviour when you have no evidence of what the
user does want.

## Why only auto-reject, never auto-approve

Auto-rejection is safe: a mistake costs one missed opportunity, and the filtered
list makes it recoverable. Auto-approval costs a bad action taken in your name
and cannot be recovered. Nothing here matures into "north submits without
asking", however high the approve rate goes.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from approval.decisions import APPROVED, REJECTED, DecisionLog
from utils.math import cosine_similarity

logger = logging.getLogger(__name__)

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]

# Evidence needed before the filter is trusted at all. Two rejections are a
# coincidence; the same reasoning as skills/retirement.py's MIN_USES.
MIN_REJECTIONS: int = 5
MIN_APPROVALS: int = 3
# How much nearer the rejections a candidate must be before it is dropped.
# A margin rather than a score, so it does not depend on how tightly a
# particular flow's candidates cluster.
DEFAULT_MARGIN: float = 0.08
# Compare against the nearest few rather than the single nearest, so one
# unusual rejection cannot start filtering a whole category.
NEIGHBOURS: int = 3


@dataclass(frozen=True)
class FilterVerdict:
    """Whether to drop a candidate, and what it was measured against."""

    filtered: bool
    reason: str = ""
    rejection_similarity: float = 0.0
    approval_similarity: float = 0.0


def _top_mean(scores: list[float], k: int) -> float:
    """Mean of the k highest scores; 0.0 when there are none."""
    if not scores:
        return 0.0
    return sum(sorted(scores, reverse=True)[:k]) / min(k, len(scores))


class RejectionFilter:
    """Scores a candidate against what you have already said yes and no to."""

    def __init__(
        self,
        log: DecisionLog,
        embed_fn: EmbedFn | None,
        *,
        margin: float = DEFAULT_MARGIN,
        min_rejections: int = MIN_REJECTIONS,
        min_approvals: int = MIN_APPROVALS,
    ) -> None:
        self._log = log
        self._embed = embed_fn
        self._margin = margin
        self._min_rejections = min_rejections
        self._min_approvals = min_approvals

    async def verdict(self, source: str, candidate: str) -> FilterVerdict:
        """Whether *candidate* is close enough to past rejections to drop.

        Returns "keep" for every reason it cannot be sure: no embeddings, too
        little evidence either way, or an embedding call that failed. Showing
        you something you did not want costs a few seconds; hiding something you
        did costs an opportunity, so uncertainty resolves towards showing it.
        """
        if self._embed is None or not candidate.strip():
            return FilterVerdict(False)

        rejected = self._log.summaries(source, REJECTED)
        approved = self._log.summaries(source, APPROVED)
        if len(rejected) < self._min_rejections or len(approved) < self._min_approvals:
            # Without both sides there is no margin to measure, only an absolute
            # score - which is the thing that does not work.
            return FilterVerdict(False)

        try:
            vectors = await self._embed([candidate, *rejected, *approved])
        except Exception:
            logger.warning("RejectionFilter: embedding failed for %r - keeping the candidate", source, exc_info=True)
            return FilterVerdict(False)
        if len(vectors) != 1 + len(rejected) + len(approved):
            return FilterVerdict(False)

        target = vectors[0]
        rejection_scores = [cosine_similarity(target, v) for v in vectors[1 : 1 + len(rejected)]]
        approval_scores = [cosine_similarity(target, v) for v in vectors[1 + len(rejected) :]]

        near_rejections = _top_mean(rejection_scores, NEIGHBOURS)
        near_approvals = _top_mean(approval_scores, NEIGHBOURS)

        if near_rejections - near_approvals >= self._margin:
            return FilterVerdict(
                True,
                reason=(
                    f"closer to {len(rejected)} things you rejected than to "
                    f"{len(approved)} you approved ({near_rejections:.2f} vs {near_approvals:.2f})"
                ),
                rejection_similarity=near_rejections,
                approval_similarity=near_approvals,
            )
        return FilterVerdict(False, rejection_similarity=near_rejections, approval_similarity=near_approvals)

    async def should_offer(self, source: str, candidate_id: str, candidate: str) -> bool:
        """True when this candidate should become a card.

        Records what it dropped, because auto-rejection is only acceptable if
        you can check what it threw away.
        """
        verdict = await self.verdict(source, candidate)
        if verdict.filtered:
            self._log.record_filtered(source, candidate_id, candidate, verdict.reason)
            return False
        return True
