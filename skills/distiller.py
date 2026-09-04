"""SkillDistiller - turn north's own recurring successes into reusable skills.

This is the edge a fixed model can't buy: north watches its successful
engineering tasks and, when the SAME kind of task succeeds repeatedly, distils
the shared procedure into a learned skill. Next time a similar task arrives, that
procedure is injected up front (see SkillSelector), so the model repeats what
worked instead of re-deriving it.

Runs as a background job (mirrors EpisodeConsolidator). It is idempotent: a
cluster whose tasks already produced a learned skill is skipped via provenance
overlap, so no watermark is needed and re-runs never duplicate skills.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

import yaml

from inference.base import InferenceRouter
from inference.models import CompletionRequest, PoolPriority
from memory.episodic import EpisodicStore
from skills.models import SKILL_FILENAME, SkillSource
from skills.registry import SkillRegistry, rejection_reason
from skills.selector import SkillSelector
from utils.math import cosine_similarity
from utils.text import strip_code_fences

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 900  # distillation is not urgent; run every 15 min
_ENGINEERING_DOMAINS = frozenset({"engineering"})
# A procedure must recur before it becomes a skill: distil only from clusters of
# at least this many successful tasks, so one-off successes never become skills.
MIN_CLUSTER_SIZE = 2
# Two episodes join the same cluster when their summaries are at least this similar.
CLUSTER_SIMILARITY = 0.60
# Hard cap on learned skills, so the library can never bloat and drown selection.
MAX_LEARNED_SKILLS = 24

_DISTILL_PROMPT = """\
You are distilling a REUSABLE SKILL from several successful software-engineering \
tasks that north completed. A skill is procedural memory: the concrete, repeatable \
steps that made this KIND of task succeed, so next time the same procedure is followed.

Here are {count} successful tasks of a similar kind:

{summaries}

Write ONE skill capturing the shared, generalizable procedure - only if there is a \
genuinely repeatable process here. Respond with a single JSON object:

{{
  "name": "short-kebab-case-name",
  "description": "Use when <trigger conditions>. One sentence, phrased as when to reach for this skill.",
  "body": "Numbered procedural steps; reference the specific tools/files that recur. No generic advice."
}}

Rules:
- The body must be PROCEDURAL and SPECIFIC (steps, tools, checks), not generic advice ("write clean code", etc.).
- The description must be a retrieval trigger ("Use when ..."), not a summary of what the skill is.
- If these tasks share no reusable procedure, respond exactly: {{"skill": null}}
- Output ONLY the JSON object, nothing else."""


class SkillDistiller:
    """Distils learned skills from clusters of similar successful episodes."""

    def __init__(
        self,
        episodic_store: EpisodicStore,
        inference_router: InferenceRouter,
        skill_registry: SkillRegistry,
        skill_selector: SkillSelector,
        learned_dir: Path,
        poll_interval_seconds: int = _POLL_INTERVAL_SECONDS,
    ) -> None:
        self._episodic_store = episodic_store
        self._inference_router = inference_router
        self._registry = skill_registry
        self._selector = skill_selector
        self._learned_dir = learned_dir
        self._poll_interval = poll_interval_seconds
        self._lock = asyncio.Lock()

    async def run(self) -> None:
        """Loop forever, distilling on each tick. Returns only on cancellation."""
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("SkillDistiller: run error, continuing")
            await asyncio.sleep(self._poll_interval)

    async def run_once(self) -> int:
        """Distil one pass and return the number of new skills written."""
        async with self._lock:
            return await self._distill_pass()

    async def _distill_pass(self) -> int:
        if self._learned_skill_count() >= MAX_LEARNED_SKILLS:
            return 0
        episodes = await self._episodic_store.list_successful(_ENGINEERING_DOMAINS)
        clusters = _cluster_by_similarity(episodes)
        already_distilled = self._distilled_task_ids()

        written = 0
        written_names: set[str] = set()
        for cluster in clusters:
            if self._learned_skill_count() + written >= MAX_LEARNED_SKILLS:
                break
            task_ids = [task_id for task_id, _ in cluster]
            if already_distilled.intersection(task_ids):
                continue  # a skill was already distilled from this pattern
            if await self._distill_and_write(cluster, written_names):
                written += 1

        if written:
            self.reload_registry()
            logger.info("SkillDistiller: wrote %d new learned skill(s)", written)
        return written

    def reload_registry(self) -> None:
        """Re-read the learned skills from disk and drop the selector's cache.

        Needed by anything that changes a skill file behind the registry's back -
        the distiller writing a new one, or the retirement sweep withdrawing one.
        Without the invalidate, a retired skill keeps being offered from cache
        until the process restarts.
        """
        self._registry.reload()
        self._selector.invalidate()

    async def _distill_and_write(self, cluster: list[tuple[str, str]], written_names: set[str]) -> bool:
        """Distil one cluster into a skill and write it. Returns True on success."""
        summaries = [summary for _, summary in cluster]
        distilled = await self._distill(summaries)
        if distilled is None:
            return False
        name, description, body = distilled
        name = _slug(name)  # normalise a sentence-y model name to a kebab identifier
        if rejection_reason(name, description, body):
            return False
        # Skip names that already exist OR were written earlier in this same pass -
        # the registry is only reloaded after the loop, so two clusters distilling to
        # the same name would otherwise overwrite each other's file.
        if name in self._registry.names() or name in written_names:
            return False
        self._write_learned_skill(name, description, body, [task_id for task_id, _ in cluster])
        written_names.add(name)
        return True

    async def _distill(self, summaries: list[str]) -> tuple[str, str, str] | None:
        """Ask the model to distil a skill from the summaries; parse to (name, desc, body)."""
        listed = "\n".join(f"{i}. {s}" for i, s in enumerate(summaries, start=1))
        prompt = _DISTILL_PROMPT.format(count=len(summaries), summaries=listed)
        try:
            response = await self._inference_router.complete(
                CompletionRequest(prompt=prompt, priority=PoolPriority.LOW, component="skill_distiller")
            )
        except Exception:
            logger.warning("SkillDistiller: distillation call failed", exc_info=True)
            return None
        return _parse_distilled(response.text)

    def _write_learned_skill(self, name: str, description: str, body: str, provenance: list[str]) -> None:
        directory = self._learned_dir / _slug(name)
        directory.mkdir(parents=True, exist_ok=True)
        # Serialize the frontmatter with yaml (not f-strings): a model-generated
        # description often contains ': ' or '#', which raw interpolation would turn
        # into invalid YAML - the skill would then fail to load, its provenance would
        # never be recorded, and the same cluster would be re-distilled every pass.
        frontmatter = yaml.safe_dump(
            {
                "name": name,
                "description": description,
                "version": "1.0.0",
                "status": "active",
                "source": SkillSource.LEARNED.value,
                "provenance": provenance,
            },
            sort_keys=False,
            allow_unicode=True,
        )
        document = f"---\n{frontmatter}---\n\n{body}\n"
        (directory / SKILL_FILENAME).write_text(document, encoding="utf-8")

    def _learned_skill_count(self) -> int:
        return sum(1 for skill in self._registry.all() if skill.source is SkillSource.LEARNED)

    def _distilled_task_ids(self) -> set[str]:
        """Every task id any learned skill was already distilled from."""
        ids: set[str] = set()
        for skill in self._registry.all():
            if skill.source is SkillSource.LEARNED:
                ids.update(skill.provenance)
        return ids


def _cluster_by_similarity(episodes: list[tuple[str, str, list[float] | None]]) -> list[list[tuple[str, str]]]:
    """Greedily group episodes whose summary embeddings are mutually similar.

    Each returned cluster is a list of (task_id, summary) and has at least
    MIN_CLUSTER_SIZE members. Episodes without an embedding are ignored, so this
    yields nothing when embeddings are unavailable.
    """
    embedded = [(task_id, summary, emb) for task_id, summary, emb in episodes if emb]
    clusters: list[list[tuple[str, str]]] = []
    used: set[int] = set()
    for i, (task_id, summary, emb) in enumerate(embedded):
        if i in used:
            continue
        cluster = [(task_id, summary)]
        used.add(i)
        for j in range(i + 1, len(embedded)):
            if j in used:
                continue
            other_id, other_summary, other_emb = embedded[j]
            if cosine_similarity(emb, other_emb) >= CLUSTER_SIMILARITY:
                cluster.append((other_id, other_summary))
                used.add(j)
        if len(cluster) >= MIN_CLUSTER_SIZE:
            clusters.append(cluster)
    return clusters


def _parse_distilled(text: str) -> tuple[str, str, str] | None:
    """Parse the model's JSON into (name, description, body), or None."""
    try:
        parsed = json.loads(strip_code_fences(text))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict) or parsed.get("skill", "") is None:
        return None
    name = str(parsed.get("name") or "").strip()
    description = str(parsed.get("description") or "").strip()
    body = str(parsed.get("body") or "").strip()
    if not (name and description and body):
        return None
    return name, description, body


def _slug(name: str) -> str:
    """A kebab-case skill name/folder, consistent with the built-in skills.

    Models sometimes return a whole sentence as the name; this normalises it to a
    short, hyphenated identifier like the hand-authored skills use.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "skill"
