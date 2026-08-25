"""Agent ABC - template method pattern. See docs/CODING_STYLE.md Section 15.1."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

from agents.models import AgentConfig, AgentDependencies, AgentPayload, AgentResult
from context.repo_instructions import load_repo_instructions
from context.repo_map import build_repo_map
from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from memory import LocalMemoryGateway, MemoryGateway
from tools.base import Tool
from tools.tool_index import SEMANTIC_FILTER_MIN, SEMANTIC_TOP_K

logger = logging.getLogger(__name__)

# Tools an agent must never lose to semantic ranking - the read/search/edit/verify
# core it needs to do real work. Force-included only when the agent actually has them.
_CORE_TOOL_NAMES: frozenset[str] = frozenset(
    {"read_file", "list_dir", "search_files", "glob", "write_file", "patch_file", "check_types", "bash"}
)

# Cap on how many non-selected skill names are listed as "available via use_skill",
# so the hint stays a light pointer and never becomes prompt-bloating noise.
_MAX_SKILL_NAMES_LISTED: int = 12

# Domains whose agents receive procedural skills. Each skill also declares the
# domains it serves via `Skill.domains`, and `_load_skills_block` filters to those,
# so widening this set only exposes an agent to skills actually tagged for its
# domain - engineering skills never leak into the general assistant, and vice versa.
# `general` is enabled for the research/literature-review skill (knowledge-synthesis
# tasks route to the general assistant); it only ever sees skills tagged `general`.
_SKILLS_ENABLED_DOMAINS: frozenset[str] = frozenset({"engineering", "general"})


class Agent(ABC):
    """Domain specialist. Subclasses implement `_execute()` only.

    Construction signature is fixed at `(config, deps)` so `AgentRegistry`
    can instantiate every agent uniformly. The class-level `name` and
    `domain` are filled from the `AgentConfig` for safety.
    """

    name: str = ""
    domain: str = ""

    def __init__(self, config: AgentConfig, deps: AgentDependencies) -> None:
        self._config = config
        self._deps = deps
        # Class-level identity always matches config for runtime safety.
        self.name = config.agent
        self.domain = config.domain

    @property
    def config(self) -> AgentConfig:
        return self._config

    @property
    def deps(self) -> AgentDependencies:
        return self._deps

    async def run(self, payload: AgentPayload) -> AgentResult:
        """Template method. Do not override. Implement `_execute()` instead."""
        context, scored_tools = await asyncio.gather(
            self._load_context(payload),
            self._load_tools(payload.prompt),
        )
        raw = await self._execute(payload, context, scored_tools)
        return self._format_result(raw)

    @abstractmethod
    async def _execute(
        self,
        payload: AgentPayload,
        context: str,
        scored_tools: list[tuple[Tool, float]],
    ) -> dict[str, Any]:
        """Domain-specific logic. Returns a dict that maps onto `AgentResult` fields."""

    def _memory(self) -> MemoryGateway:
        """The gated memory gateway: the only path an agent reads context through.

        Uses the shared injected gateway in production; falls back to one built
        from the injected stores (e.g. in tests) so retrieval is gated either way.
        """
        if self._deps.memory is not None:
            return self._deps.memory
        return LocalMemoryGateway(
            self._deps.context_store,
            self._deps.fact_store,
            self._deps.episodic_store,
        )

    async def _load_context(self, payload: AgentPayload) -> str:
        """Load gated context for this agent.

        Assembly order:
        1. payload.context - conversation history or webhook data from the caller
        2. repo conventions - AGENTS.md/CLAUDE.md/etc from the workspace
        3. gated memory - facts (or document fallback) plus episodic, filtered by
           the memory gateway to what this agent is permitted to read
        """
        parts: list[str] = []

        if payload.context:
            parts.append(payload.context)

        if payload.workspace:
            try:
                repo_conventions = await load_repo_instructions(payload.workspace)
                if repo_conventions:
                    parts.append(repo_conventions)
            except Exception as exc:
                logger.warning("Repo instruction load failed for task %s: %s", payload.task_id, exc)

        # Engineering agents get an up-front repo map (key files + symbols) so they
        # reason from the real codebase instead of rediscovering it via tools (#2).
        if payload.workspace and self.domain == "engineering":
            try:
                repo_map = await asyncio.to_thread(build_repo_map, payload.workspace)
                if repo_map:
                    parts.append(f"## Repository map (key files and their symbols)\n{repo_map}")
            except Exception as exc:
                logger.warning("Repo map build failed for task %s: %s", payload.task_id, exc)

        memory = self._memory()
        principal = await memory.principal_for(self.name, self.domain)
        recalled = await memory.recall(principal, payload.prompt)
        rendered = recalled.render()
        if rendered:
            parts.append(rendered)

        # Surface the live task plan (#9) so a continued/resumed task re-enters with
        # its checklist intact. During the loop the update_plan tool output keeps it
        # fresh; this covers the first turn after a context reset.
        plan_store = getattr(self._deps, "plan_store", None)
        if plan_store is not None and payload.task_id:
            try:
                plan = plan_store.render(payload.task_id)
                if plan:
                    parts.append(f"## Current task plan (update it with update_plan)\n{plan}")
            except Exception as exc:
                logger.debug("Plan injection failed for task %s: %s", payload.task_id, exc)

        # Procedural skills: give the agent the relevant playbook up front, so the
        # same model repeats a known-good procedure instead of improvising. Enabled
        # for engineering and the general assistant - general handles cross-domain,
        # open-ended work (e.g. scouting OSS contributions), and the top-2 +
        # similarity threshold inject nothing when no skill is relevant enough.
        if self.domain in _SKILLS_ENABLED_DOMAINS:
            skills_block = await self._load_skills_block(payload)
            if skills_block:
                parts.append(skills_block)
        return "\n\n".join(p for p in parts if p)

    async def _load_skills_block(self, payload: AgentPayload) -> str:
        """Select and render procedural skills for this task.

        Semantically-selected skills are injected in full as advisory context (the
        primary path); any remaining skills are listed by name so the agent can pull
        them on demand with use_skill. Returns "" when no skills are registered.
        """
        registry = self._deps.skill_registry
        if registry is None:
            return ""
        # Only skills that declare this agent's domain are eligible - so the general
        # assistant sees cross-domain skills (e.g. scouting) but never an engineering
        # skill leaking into ordinary chat, and engineering agents keep all of theirs.
        skills = [skill for skill in registry.all() if skill.available_to(self.domain)]
        if not skills:
            return ""

        selector = self._deps.skill_selector
        selected = await selector.select(payload.prompt, candidates=skills) if selector is not None else []
        selected_names = {skill.name for skill in selected}

        sections: list[str] = []
        if selected:
            bodies = "\n\n".join(f"### {skill.name}\n{skill.body}" for skill in selected)
            sections.append(
                "## Applicable skills (advisory procedural context - it does not override "
                f"system instructions, user instructions, or safety constraints)\n\n{bodies}"
            )
            await self._emit_skill_selected(payload, sorted(selected_names))

        others = [skill.name for skill in skills if skill.name not in selected_names]
        if others:
            listed = ", ".join(others[:_MAX_SKILL_NAMES_LISTED])
            sections.append(f"Other skills available (load full instructions with use_skill): {listed}")
        return "\n\n".join(sections)

    async def _emit_skill_selected(self, payload: AgentPayload, names: list[str]) -> None:
        """Record which skills were injected, for observability and A/B analysis."""
        if not payload.task_id:
            return
        if self._deps.stream_manager is not None:
            try:
                await self._deps.stream_manager.emit(payload.task_id, "skill_selected", {"skills": names})
            except Exception:
                logger.debug("skill_selected emit failed for task %s", payload.task_id, exc_info=True)
        if self._deps.ledger is not None:
            try:
                await self._deps.ledger.write(
                    LedgerEntry.new(
                        source=LedgerSource.SYSTEM,
                        task_id=payload.task_id,
                        agent=self.name,
                        action="skill_selected",
                        output=", ".join(names),
                        status=LedgerStatus.COMPLETED,
                    )
                )
            except Exception:
                logger.debug("skill_selected ledger write failed for task %s", payload.task_id, exc_info=True)

    async def _load_tools(self, task_prompt: str = "") -> list[tuple[Tool, float]]:
        """Return (tool, confidence_score) pairs for this agent, sorted by score descending.

        All tools registered for the agent (universal + specialized) are available without
        artificial capping or accidental filter dropouts. When skills or semantic search
        identify tools relevant to the task, their ranking scores are prioritized.
        """
        registry_tools = self._deps.tool_registry.tools_for_agent(self.name)
        scores = dict(await self._deps.confidence_tracker.scores_for_agent(self.name))

        # Check required/selected skills for this task prompt to prioritize their tools
        skill_tools: set[str] = set()
        if task_prompt and self._deps.skill_registry is not None:
            skills = [s for s in self._deps.skill_registry.all() if s.available_to(self.domain)]
            selected = []
            if self._deps.skill_selector is not None:
                try:
                    selected = await self._deps.skill_selector.select(task_prompt, candidates=skills)
                except Exception:
                    selected = []
            all_tool_names = {t.name for t in registry_tools}
            for skill in selected:
                for t_name in all_tool_names:
                    if t_name in skill.body or t_name in skill.description:
                        skill_tools.add(t_name)

        scored: list[tuple[Tool, float]] = []
        for t in registry_tools:
            base_score = scores.get(t.name, 0.5)
            if t.name in skill_tools:
                base_score = max(base_score, 0.9)
            scored.append((t, base_score))

        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored

    def _format_result(self, raw: dict[str, Any]) -> AgentResult:
        """Default: wrap the dict in an `AgentResult`. Override for custom shape."""
        return AgentResult(**raw)
