"""SkillSelector - pick the most relevant skills for a task by semantic similarity.

The point of the whole subsystem: put the right procedural knowledge in front of
the model *before* it acts, rather than hoping a weak model asks for it. Selection
is deliberately conservative - at most a couple of skills, only above a similarity
threshold - because irrelevant injected context degrades weak models more than it
helps. When embeddings are unavailable it selects nothing, so the caller injects
noise-free context and the optional ``use_skill`` tool remains the fallback.
"""

from __future__ import annotations

import logging

from config.dependencies import EmbedFn
from skills.models import Skill
from skills.registry import SkillRegistry
from utils.math import cosine_similarity

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 2  # inject at most this many skills - more context hurts weak models
DEFAULT_MIN_SIMILARITY = 0.35  # below this, no skill is relevant enough to be worth injecting


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
        self._embeddings: dict[str, list[float]] | None = None  # name -> vector, built lazily

    async def select(self, task_text: str, candidates: list[Skill] | None = None) -> list[Skill]:
        """Return up to ``top_k`` skills most similar to ``task_text``, above threshold.

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
        return [skill for score, skill in scored[: self._top_k] if score >= self._min_similarity]

    def invalidate(self) -> None:
        """Drop cached embeddings so the next select re-embeds (after the set changes)."""
        self._embeddings = None

    async def _skill_embeddings(self, skills: list[Skill]) -> dict[str, list[float]]:
        """Embed each skill's retrieval key once, rebuilding when the skill set changes."""
        names = {skill.name for skill in skills}
        if self._embeddings is not None and set(self._embeddings) == names:
            return self._embeddings
        vectors = await self._embed_fn([_retrieval_key(skill) for skill in skills])
        self._embeddings = {skill.name: vec for skill, vec in zip(skills, vectors, strict=False)}
        return self._embeddings


def _retrieval_key(skill: Skill) -> str:
    """The text a skill is matched on: its name plus its trigger-oriented description."""
    return f"{skill.name}: {skill.description}"
