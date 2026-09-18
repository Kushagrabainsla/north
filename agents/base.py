"""Agent ABC - template method pattern. See docs/CODING_STYLE.md Section 15.1."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Any

from agents.models import AgentConfig, AgentDependencies, AgentPayload, AgentResult
from context.repo_instructions import load_repo_instructions
from context.repo_map import build_repo_map
from context.repo_revision import repository_identity
from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from memory import LocalMemoryGateway, MemoryGateway
from tools.base import Tool
from tools.retrieval import tool_index_documents
from tools.tool_index import SEMANTIC_FILTER_MIN, SEMANTIC_TOP_K
from utils.execution_context import ExecutionIdentity, bind_execution

logger = logging.getLogger(__name__)

# Registry tools that support the agent loop itself. Internal tools such as
# ask_user, delegate_task, request_approval, and find_tools are injected by the
# loop and therefore do not appear here.
_ESSENTIAL_TOOL_NAMES: frozenset[str] = frozenset({"update_plan", "use_skill"})

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


def _mentioned_tool_names(skills: list[Any], tool_names: set[str]) -> set[str]:
    """Tool names named as whole words in any of *skills*' text.

    Substring matching over-fires ("global" contains "glob"), which promoted
    tools a skill never actually calls above ones it does.
    """
    if not skills or not tool_names:
        return set()
    corpus = " ".join(f"{skill.body} {skill.description}" for skill in skills)
    words = set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", corpus))
    return tool_names & words


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
        identity = ExecutionIdentity(payload.run_id, payload.parent_run_id, payload.attempt)
        store = self._deps.agent_run_store
        if store is not None:
            await store.start(payload, self.name)
        started = time.monotonic()
        with bind_execution(identity):
            try:
                # Selected once and shared: _load_context and _load_tools both need
                # the task's skills, and each selection costs an embedding call.
                selected_skills = await self._select_skills(payload.prompt)
                context, scored_tools = await asyncio.gather(
                    self._load_context(payload, selected_skills),
                    self._load_tools(payload, selected_skills),
                )
                raw = await self._execute(payload, context, scored_tools)
                result = self._format_result(raw).model_copy(
                    update={
                        "run_id": payload.run_id,
                        "parent_run_id": payload.parent_run_id,
                        "attempt": payload.attempt,
                    }
                )
                if result.duration_ms is None:
                    result.duration_ms = int((time.monotonic() - started) * 1000)
                if store is not None:
                    await store.complete(payload.run_id, result)
                return result
            except asyncio.CancelledError:
                if store is not None:
                    await store.finish_with_error(payload.run_id, "cancelled", "Agent run cancelled")
                raise
            except Exception as exc:
                if store is not None:
                    await store.finish_with_error(payload.run_id, "failed", str(exc))
                raise

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

    async def _select_skills(self, task_prompt: str) -> list[Any]:
        """Skills relevant to this task, selected once per run.

        Selection embeds the prompt, so it is done here and passed to both
        `_load_context` (which offers the skills by description) and `_load_tools`
        (which boosts the tools those skills name) rather than run twice.
        """
        registry = self._deps.skill_registry
        selector = self._deps.skill_selector
        if registry is None or selector is None or not task_prompt:
            return []
        candidates = [skill for skill in registry.all() if skill.available_to(self.domain)]
        if not candidates:
            return []
        try:
            return await selector.select(task_prompt, candidates=candidates)
        except Exception:
            logger.debug("Skill selection failed for agent %s", self.name, exc_info=True)
            return []

    async def _load_context(self, payload: AgentPayload, selected_skills: list[Any] | None = None) -> str:
        """Load gated context for this agent.

        Assembly order:
        1. payload.context - conversation history or webhook data from the caller
        2. repo conventions - AGENTS.md/CLAUDE.md/etc from the workspace
        3. gated memory - facts (or document fallback) plus episodic, filtered by
           the memory gateway to what this agent is permitted to read

        `selected_skills` is the run's already-selected skills, passed by `run()`
        so selection happens once. `None` means "not selected yet" and this method
        selects them itself, so it stays correct when called on its own; an empty
        list means selection ran and chose nothing.
        """
        parts: list[str] = []
        sections: dict[str, str] = {}

        def add_section(name: str, value: str) -> None:
            if not value:
                return
            parts.append(value)
            sections[name] = value

        if payload.context:
            add_section("caller_context", payload.context)

        if payload.workspace:
            try:
                repo_conventions = await load_repo_instructions(payload.workspace)
                if repo_conventions:
                    add_section("repository_instructions", repo_conventions)
            except Exception as exc:
                logger.warning("Repo instruction load failed for task %s: %s", payload.task_id, exc)

        # Engineering agents get an up-front repo map (key files + symbols) so they
        # reason from the real codebase instead of rediscovering it via tools (#2).
        if payload.workspace and self.domain == "engineering":
            try:
                identity = await asyncio.to_thread(repository_identity, payload.workspace)
                if identity is not None:
                    add_section("repository_revision", identity.render())
                repo_map = await asyncio.to_thread(build_repo_map, payload.workspace)
                if repo_map:
                    add_section("repository_map", f"## Repository map (key files and their symbols)\n{repo_map}")
            except Exception as exc:
                logger.warning("Repo map build failed for task %s: %s", payload.task_id, exc)

        memory = self._memory()
        principal_for = memory.principal_for
        # Keep compatibility with lightweight memory gateways supplied by callers
        # and older integrations that predate workspace-aware principals.
        if "workspace" in inspect.signature(principal_for).parameters:
            principal = await principal_for(self.name, self.domain, workspace=payload.workspace)
        else:
            principal = await principal_for(self.name, self.domain)
        recalled = await memory.recall(principal, payload.prompt)
        await self._emit_memory_recall(payload, principal, recalled)
        rendered = recalled.render()
        if rendered:
            add_section("memory", rendered)

        # Surface the live task plan (#9) so a continued/resumed task re-enters with
        # its checklist intact. During the loop the update_plan tool output keeps it
        # fresh; this covers the first turn after a context reset.
        plan_store = getattr(self._deps, "plan_store", None)
        if plan_store is not None and payload.task_id:
            try:
                plan = plan_store.render(payload.task_id)
                if plan:
                    add_section("task_plan", f"## Current task plan (update it with update_plan)\n{plan}")
            except Exception as exc:
                logger.debug("Plan injection failed for task %s: %s", payload.task_id, exc)

        # Procedural skills: give the agent the relevant playbook up front, so the
        # same model repeats a known-good procedure instead of improvising. Enabled
        # for engineering and the general assistant - general handles cross-domain,
        # open-ended work (e.g. scouting OSS contributions), and the top-2 +
        # similarity threshold inject nothing when no skill is relevant enough.
        if self.domain in _SKILLS_ENABLED_DOMAINS:
            if selected_skills is None:
                selected_skills = await self._select_skills(payload.prompt)
            skills_block = await self._load_skills_block(payload, selected_skills)
            if skills_block:
                add_section("skills", skills_block)
        payload.context_sections = sections
        return "\n\n".join(p for p in parts if p)

    async def _emit_memory_recall(self, payload: AgentPayload, principal: Any, recalled: Any) -> None:
        """Record retrieval shape without persisting any recalled text."""
        telemetry_fn = getattr(recalled, "telemetry", None)
        stats = telemetry_fn() if callable(telemetry_fn) else {
            "source_categories": [],
            "source_counts": {"facts": 0, "episodes": 0, "documents": 0},
            "source_characters": {"facts": 0, "episodes": 0, "documents": 0},
            "total_items": 0,
            "total_characters": 0,
            "estimated_tokens": 0,
        }
        fact_topics = getattr(principal, "allowed_fact_topics", None)
        episode_domains = getattr(principal, "allowed_domains", frozenset())
        data = {
            **stats,
            "fact_scope": ["*"] if fact_topics is None else sorted(fact_topics),
            "episode_scope": sorted(episode_domains),
        }
        if self._deps.stream_manager is not None and payload.task_id:
            await self._deps.stream_manager.emit(payload.task_id, "memory_recalled", data)
        elif self._deps.agent_run_store is not None:
            await self._deps.agent_run_store.record_event(
                payload.run_id,
                payload.task_id,
                "memory_recalled",
                data,
            )

    async def _load_skills_block(self, payload: AgentPayload, selected: list[Any]) -> str:
        """Offer the most relevant procedural skills for this task, by description.

        Only each skill's one-line description reaches the prompt; the full
        procedure is fetched on demand with ``use_skill``. Pasting the bodies in
        instead cost ~1,500 tokens of every message and up to 4,000 in the worst
        case - and because the whole opening block is re-sent on every turn of the
        agent loop, that was paid twenty-odd times per task for a procedure the
        model often did not end up needing. A description is ~50.

        The trade is one extra round trip when a skill *is* wanted. That is worth
        it when the alternative is carrying every candidate playbook, in full,
        through the entire conversation.

        Returns "" when no skills are registered for this agent's domain.
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

        selected_names = {skill.name for skill in selected}

        sections: list[str] = []
        if selected:
            offered = "\n".join(f"- {skill.name}: {skill.description}" for skill in selected)
            sections.append(
                "## Skills for this task\n"
                "Each line is a procedure north has for this kind of work. When one matches what "
                "you are about to do, call use_skill with its name to get the full instructions "
                "before you act. They are advisory: they do not override system instructions, "
                f"user instructions, or safety constraints.\n\n{offered}"
            )
            await self._emit_skill_selected(payload, selected)

        others = [skill.name for skill in skills if skill.name not in selected_names]
        if others:
            listed = ", ".join(others[:_MAX_SKILL_NAMES_LISTED])
            sections.append(f"Other skills available (load full instructions with use_skill): {listed}")
        return "\n\n".join(sections)

    async def _emit_skill_selected(self, payload: AgentPayload, selected: list[Any]) -> None:
        """Record which skills were injected, for observability and A/B analysis."""
        if not payload.task_id:
            return
        names = sorted(skill.name for skill in selected)
        versions = [
            {"name": skill.name, "version": skill.version, "source": skill.source.value}
            for skill in sorted(selected, key=lambda item: item.name)
        ]
        if self._deps.agent_run_store is not None:
            await self._deps.agent_run_store.set_skills(payload.run_id, versions)
        if self._deps.stream_manager is not None:
            try:
                await self._deps.stream_manager.emit(
                    payload.task_id, "skill_selected", {"skills": names, "skill_versions": versions}
                )
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

    async def _load_tools(
        self,
        payload: AgentPayload | None = None,
        selected_skills: list[Any] | None = None,
    ) -> list[tuple[Tool, float]]:
        """Select a lean task-relevant subset from the global tool catalog.

        Every registered tool is eligible. Semantic retrieval limits prompt
        size, while loop controls, skill-named tools, and explicitly named tools
        cannot be dropped. If indexing is unavailable,
        all tools are exposed so selection failure never removes a capability.
        """
        registry_tools = self._deps.tool_registry.available_tools()
        by_name = {tool.name: tool for tool in registry_tools}
        all_names = set(by_name)
        prompt = payload.prompt if payload is not None else ""

        mandatory = set(_ESSENTIAL_TOOL_NAMES) & all_names
        mandatory.update(_mentioned_tool_names(selected_skills or [], all_names))
        mandatory.update(_tool_names_in_text(prompt, all_names))

        selected_names = set(all_names)
        index = self._deps.tool_index
        if index is not None and len(registry_tools) > SEMANTIC_FILTER_MIN:
            query = self._tool_selection_query(payload, selected_skills or [])
            try:
                # Keeps tools hot-loaded after startup in the same searchable
                # catalog without making registration depend on the index.
                await index.update_tools(tool_index_documents(registry_tools))
                semantic_names = await index.search_tools(query, top_k=SEMANTIC_TOP_K)
            except Exception:
                logger.debug("Tool selection failed for agent %s", self.name, exc_info=True)
                semantic_names = []
            semantic_matches = set(semantic_names) & all_names
            if semantic_matches:
                selected_names = semantic_matches | mandatory

        scores = dict(await self._deps.confidence_tracker.scores_for_agent(self.name))

        scored: list[tuple[Tool, float]] = []
        for name in selected_names:
            t = by_name[name]
            base_score = scores.get(t.name, 0.5)
            if t.name in mandatory:
                base_score = max(base_score, 0.9)
            scored.append((t, base_score))

        scored.sort(key=lambda pair: (-pair[1], pair[0].name))
        return scored

    def _tool_selection_query(self, payload: AgentPayload | None, selected_skills: list[Any]) -> str:
        """Build retrieval text from dynamic task context, never static ownership."""
        parts = [f"Agent role: {self.name}. Domain: {self.domain}."]
        if payload is not None:
            parts.append(f"Task: {payload.prompt}")
            plan_store = getattr(self._deps, "plan_store", None)
            if plan_store is not None and payload.task_id:
                try:
                    if plan := plan_store.render(payload.task_id):
                        parts.append(f"Current plan: {plan}")
                except Exception:
                    logger.debug("Plan unavailable during tool selection for %s", payload.task_id, exc_info=True)
        if selected_skills:
            parts.append(
                "Selected skills: "
                + " ".join(f"{skill.name}: {skill.description}" for skill in selected_skills)
            )
        return "\n".join(parts)

    def _format_result(self, raw: dict[str, Any]) -> AgentResult:
        """Default: wrap the dict in an `AgentResult`. Override for custom shape."""
        return AgentResult(**raw)


def _tool_names_in_text(text: str, tool_names: set[str]) -> set[str]:
    """Return exact tool identifiers named in task text."""
    if not text or not tool_names:
        return set()
    words = set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", text))
    return tool_names & words
