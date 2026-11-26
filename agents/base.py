"""Agent ABC - template method pattern. See docs/CODING_STYLE.md Section 15.1."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from agents.models import AgentConfig, AgentDependencies, AgentPayload, AgentResult
from context.repo_instructions import load_repo_instructions
from context.repo_map import build_repo_map
from context.repo_revision import repository_identity
from inference.exceptions import is_model_unavailable_error
from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from memory import LocalMemoryGateway, MemoryGateway
from memory.memory_use import judge_later
from tools.base import Tool
from utils.execution_context import ExecutionIdentity, bind_execution
from utils.tools import named_tools

logger = logging.getLogger(__name__)

# The tools every run starts with: the loop's own controls and the tools north uses
# most. With only the controls, bash, fetch_url and web_search were missing in chat
# tasks: the calls failed "not found", and north told the user it had no shell (issue
# #82, item 3). browser was in 12 of 107 chat runs and 227 calls (2026-11-10 to 11-22);
# create_skill and create_flow, next, in 6 and 5. The set is fixed so the cached prompt
# stays the same across tasks. A flow step's tool list replaces it. Internal tools
# such as ask_user, request_approval and find_tools are injected by the loop.
_ESSENTIAL_TOOL_NAMES: frozenset[str] = frozenset(
    {"update_plan", "use_skill", "bash", "write_file", "fetch_url", "web_search", "browser"}
)

# The task model chooses its skill from every skill of its domain, by name and description, as in the
# open Agent Skills format; north no longer suggests one by similarity. On 168 real chat requests (50 need
# a skill, by a judge that read every skill in full), a similarity suggestion at the 0.70 floor named the
# right skill for 7; with that suggestion and the other skills listed by name only, Opus 5.5 loaded the
# right one for 36; with every name and description listed, for 47 (experiments/skill_picking, issue #87).

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
    return named_tools(" ".join(f"{skill.body} {skill.description}" for skill in skills), tool_names)


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
        # The memory each running run was given, by run id, so its use can be judged when it ends (#85).
        self._recalled: dict[str, Any] = {}

    @property
    def config(self) -> AgentConfig:
        return self._config

    @property
    def deps(self) -> AgentDependencies:
        return self._deps

    async def run(self, payload: AgentPayload) -> AgentResult:
        """Template method. Do not override. Implement `_execute()` instead."""
        store = self._deps.agent_run_store
        # A task that resumes continues this step's run where it stopped (CODING_STYLE §15.5).
        stopped = (
            await store.continuable_run(payload.task_id, self.name, payload.prompt)
            if store is not None and payload.parent_run_id is None
            else None
        )
        if stopped is not None:
            payload = payload.model_copy(update={"run_id": stopped})
        identity = ExecutionIdentity(payload.run_id, payload.parent_run_id, payload.attempt)
        if store is not None:
            await store.start(payload, self.name)
        started = time.monotonic()
        with bind_execution(identity):
            try:
                # Selected once and shared: _load_context and _load_tools both need
                # the task's skills, and each selection costs an embedding call.
                selected_skills = await self._select_skills(
                    payload.prompt, payload.skills, allow_candidates=payload.allow_candidate_skills
                )
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
                self._judge_memory_use(payload, result.output)
                return result
            except asyncio.CancelledError:
                if store is not None:
                    await store.finish_with_error(payload.run_id, "cancelled", "Agent run cancelled")
                raise
            except Exception as exc:
                if store is not None:
                    # No model could take the next step: the run's work stands, and the task, queued until a
                    # model is back, continues it. Recorded as failed, it started over and forgot what it did.
                    status = "paused" if is_model_unavailable_error(exc) else "failed"
                    await store.finish_with_error(payload.run_id, status, str(exc))
                raise
            finally:
                self._recalled.pop(payload.run_id or "", None)

    def _judge_memory_use(self, payload: AgentPayload, answer: str) -> None:
        """Start the background check of which recalled items this run used (#85)."""
        recalled = self._recalled.get(payload.run_id or "")
        items_fn = getattr(recalled, "items", None)
        if not callable(items_fn):
            return
        judge_later(
            self._deps.inference_router,
            self._deps.agent_run_store,
            run_id=payload.run_id,
            task_id=payload.task_id,
            task=payload.prompt,
            answer=answer,
            items=items_fn(),
            counts=getattr(self._memory(), "use_store", None),
        )

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

    async def _select_skills(
        self, task_prompt: str, required_names: list[str] | None = None, *, allow_candidates: bool = False
    ) -> list[Any]:
        """The skills a flow step requires, checked once per run; [] for any other task.

        Passed to both `_load_context` (which gives their procedures in full) and
        `_load_tools` (which adds the tools they name). Any other task chooses its
        own skill from the list in its context and loads it with use_skill.
        """
        if not required_names:
            return []
        registry = self._deps.skill_registry
        if registry is None:
            raise ValueError("Required skills cannot run because the skill registry is unavailable")
        selected = []
        for name in dict.fromkeys(required_names):
            skill = registry.get(name)
            candidate_allowed = allow_candidates and skill.status == "candidate" and self.domain in skill.domains
            if not skill.available_to(self.domain) and not candidate_allowed:
                raise ValueError(f"Skill {name!r} is not active for agent {self.name!r} in domain {self.domain!r}")
            selected.append(skill)
        return selected

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
        if payload.run_id:
            self._recalled[payload.run_id] = recalled
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

        # Procedural skills, so the model repeats a known-good procedure instead of
        # improvising. Engineering and general agents get the list of their skills;
        # a flow's required skill is given in full to any compatible agent domain.
        if payload.skills or self.domain in _SKILLS_ENABLED_DOMAINS:
            if selected_skills is None:
                selected_skills = await self._select_skills(
                    payload.prompt, payload.skills, allow_candidates=payload.allow_candidate_skills
                )
            skills_block = await self._load_skills_block(payload, selected_skills)
            if skills_block:
                add_section("skills", skills_block)
        payload.context_sections = sections
        return "\n\n".join(p for p in parts if p)

    async def _emit_memory_recall(self, payload: AgentPayload, principal: Any, recalled: Any) -> None:
        """Record retrieval shape without persisting any recalled text."""
        telemetry_fn = getattr(recalled, "telemetry", None)
        stats = (
            telemetry_fn()
            if callable(telemetry_fn)
            else {
                "source_categories": [],
                "source_counts": {"facts": 0, "episodes": 0, "documents": 0},
                "source_characters": {"facts": 0, "episodes": 0, "documents": 0},
                "total_items": 0,
                "total_characters": 0,
                "estimated_tokens": 0,
            }
        )
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
        """List every skill of this agent's domain by name and description; give a flow's required skills in full.

        Only each skill's description reaches the prompt; the full procedure is
        fetched on demand with ``use_skill``. Pasting the bodies in instead cost
        ~1,500 tokens of every message and up to 4,000 in the worst case, re-sent
        on every turn of the agent loop. A description is ~40 tokens. The list is
        sorted by name, so it is the same text in every task.

        Returns "" when no skills are registered for this agent's domain.
        """
        registry = self._deps.skill_registry
        if registry is None:
            return ""
        # Only skills that declare this agent's domain are eligible - so the general
        # assistant sees cross-domain skills (e.g. scouting) but never an engineering
        # skill leaking into ordinary chat, and engineering agents keep all of theirs.
        skills = [skill for skill in registry.all() if skill.available_to(self.domain)]
        if not skills and not (payload.skills and selected):
            return ""

        if payload.skills:
            required = [
                f"## Required skill: {skill.name}\n{skill.description}\n\n{skill.body.strip()}" for skill in selected
            ]
            await self._emit_skill_selected(payload, selected)
            return (
                "# Required flow procedures\n"
                "Execute the procedures below for this flow step. They are the execution contract, "
                "not optional suggestions. Follow the step's server-enforced mutation policy and do not "
                "substitute a different procedure.\n\n" + "\n\n".join(required)
            )
        listed = "\n".join(f"- {skill.name}: {skill.description}" for skill in sorted(skills, key=lambda s: s.name))
        return (
            "## Skills\n"
            "Each line is a procedure north has for one kind of work: its name and when to use it. "
            "If one fits what you are about to do, call use_skill with its name to get the full "
            "instructions before you act; if none fits, use none. They are advisory: they do not "
            f"override system instructions, user instructions, or safety constraints.\n\n{listed}"
        )

    async def record_skill_loaded(self, payload: AgentPayload, name: str) -> None:
        """Record a skill the task loaded with use_skill, as a required skill is recorded."""
        registry = self._deps.skill_registry
        if registry is None:
            return
        try:
            skill = registry.get(name)
        except Exception:
            return
        await self._emit_skill_selected(payload, [skill])

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
        """The tools a run starts with: the same set for every task, plus any the task names.

        Tool definitions come first in a cached prompt, so a different set on each task
        wrote the whole prefix again and never read it. A similarity search picked the
        set per task: of 77 first calls within 5 minutes of another, 73 had a different
        set and read 5.4% of their prompt from cache (2026-11-12 to 11-22). A run reaches
        any other tool by calling it by name, or through find_tools. A flow step's tool
        list is a contract and replaces this set.
        """
        registry_tools = self._deps.tool_registry.available_tools()
        by_name = {tool.name: tool for tool in registry_tools}
        all_names = set(by_name)
        prompt = payload.prompt if payload is not None else ""
        scores = dict(await self._deps.confidence_tracker.scores_for_agent(self.name))

        if payload is not None and payload.allowed_tools is not None:
            # Executable skill contracts are a hard boundary, not retrieval
            # hints. Unknown names are rejected during flow validation; keeping
            # the intersection here makes the runtime fail closed as well.
            allowed = set(payload.allowed_tools) & all_names
            return sorted(
                ((by_name[name], scores.get(name, 0.5)) for name in allowed),
                key=lambda pair: (-pair[1], pair[0].name),
            )

        names = set(_ESSENTIAL_TOOL_NAMES) & all_names
        names.update(_mentioned_tool_names(selected_skills or [], all_names))
        names.update(_tool_names_in_text(prompt, all_names))
        if payload is not None and payload.workspace:
            names.update(t.name for t in registry_tools if t.task_note(payload.workspace))
        return sorted(
            ((by_name[name], max(scores.get(name, 0.5), 0.9)) for name in names),
            key=lambda pair: (-pair[1], pair[0].name),
        )

    def _format_result(self, raw: dict[str, Any]) -> AgentResult:
        """Default: wrap the dict in an `AgentResult`. Override for custom shape."""
        return AgentResult(**raw)


def _tool_names_in_text(text: str, tool_names: set[str]) -> set[str]:
    """Return exact tool identifiers named in task text."""
    if not text or not tool_names:
        return set()
    return named_tools(text, tool_names)
