"""SkillSelector - pick the most relevant skills for a task by semantic similarity.

The point of the whole subsystem: put the right procedural knowledge in front of
the model *before* it acts, rather than hoping a weak model goes looking for it.
Selection stays conservative - one primary skill and, at most, one meaningfully
different secondary skill, only above a similarity threshold - because an
irrelevant suggestion is worse than none. When embeddings are unavailable it
selects nothing, and ``use_skill`` remains the fallback.
"""

from __future__ import annotations

import logging

from inference.models import EmbedFn
from skills.models import Skill
from skills.registry import SkillRegistry
from utils.math import cosine_similarity

logger = logging.getLogger(__name__)

# One primary procedure is normally enough. A second can cover another capability
# in a compound task; more alternatives dilute the instruction rather than help.
DEFAULT_TOP_K = 2
DEFAULT_MIN_SIMILARITY = 0.35  # below this, no skill is relevant enough to be worth injecting
# Skill descriptions above this cosine similarity are treated as variants of the
# same capability. This is deliberately only a diversity filter; task-to-skill
# intent matching remains the selector's separate retrieval concern.
MAX_SECONDARY_SIMILARITY = 0.82


class SkillSelector:
    """Ranks skills against a task prompt using cached description embeddings."""

    def __init__(
        self,
        registry: SkillRegistry,
        embed_fn: EmbedFn | None,
        top_k: int = DEFAULT_TOP_K,
        min_similarity: float = DEFAULT_MIN_SIMILARITY,
    ) -> None:
        self._registry = registry
        self._embed_fn = embed_fn
        self._top_k = top_k
        self._min_similarity = min_similarity
        # name -> (retrieval text, vector), built lazily. Keeping the text with
        # the vector makes cache correctness independent of which write path
        # changed a skill and whether that caller remembered to invalidate us.
        self._embeddings: dict[str, tuple[str, list[float]]] | None = None

    async def select(self, task_text: str, candidates: list[Skill] | None = None) -> list[Skill]:
        """Return a primary skill and optionally one distinct secondary skill.

        ``candidates`` restricts scoring to a caller-provided subset (e.g. only the
        skills eligible for the caller's domain); it defaults to every registered
        skill. Returns [] when embeddings are unavailable or nothing clears the
        threshold, so the caller injects nothing rather than irrelevant noise.
        """
        if self._embed_fn is None or not task_text.strip():
            return []
        skills = self._registry.all() if candidates is None else candidates
        if not skills:
            return []
        try:
            embeddings = await self._skill_embeddings(skills)
            query = await self._embed_fn([task_text])
        except Exception:
            logger.debug("SkillSelector: embedding failed - selecting no skill", exc_info=True)
            return []
        if not query or not embeddings:
            return []

        query_vec = query[0]
        scored = [
            (cosine_similarity(query_vec, embeddings[skill.name]), skill)
            for skill in skills
            if skill.name in embeddings
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        eligible = [(score, skill) for score, skill in scored if score >= self._min_similarity]
        if not eligible or self._top_k <= 0:
            return []

        primary = eligible[0][1]
        selected = [primary]
        if self._top_k == 1:
            return selected

        primary_vec = embeddings[primary.name]
        for _score, candidate in eligible[1:]:
            capability_similarity = cosine_similarity(primary_vec, embeddings[candidate.name])
            if capability_similarity <= MAX_SECONDARY_SIMILARITY:
                selected.append(candidate)
                break
        return selected

    def invalidate(self) -> None:
        """Drop cached embeddings so the next select re-embeds (after the set changes)."""
        self._embeddings = None

    async def _skill_embeddings(self, skills: list[Skill]) -> dict[str, list[float]]:
        """Embed each skill's retrieval key once, incrementally caching embeddings."""
        if self._embeddings is None:
            self._embeddings = {}
        keys = {skill.name: _retrieval_key(skill) for skill in skills}
        missing = [
            skill
            for skill in skills
            if skill.name not in self._embeddings or self._embeddings[skill.name][0] != keys[skill.name]
        ]
        if missing:
            vectors = await self._embed_fn([keys[skill.name] for skill in missing])
            for skill, vec in zip(missing, vectors, strict=False):
                self._embeddings[skill.name] = (keys[skill.name], vec)
        return {
            name: self._embeddings[name][1]
            for name in keys
            if name in self._embeddings and self._embeddings[name][0] == keys[name]
        }


def _retrieval_key(skill: Skill) -> str:
    """The text a skill is matched on: its name plus its trigger-oriented description."""
    return f"{skill.name}: {skill.description}"
