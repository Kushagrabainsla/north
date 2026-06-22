"""Agent ABC - template method pattern. See docs/CODING_STYLE.md Section 15.1."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from agents.models import AgentConfig, AgentDependencies, AgentPayload, AgentResult
from context.repo_instructions import load_repo_instructions
from memory import ContextDocument, LocalMemoryGateway, MemoryGateway
from tools.base import Tool
from tools.tool_index import SEMANTIC_FILTER_MIN, SEMANTIC_TOP_K

logger = logging.getLogger(__name__)


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
        context = await self._load_context(payload)
        scored_tools = await self._load_tools(payload.prompt)
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

        memory = self._memory()
        principal = await memory.principal_for(self.name, self.domain)
        recalled = await memory.recall(principal, payload.prompt)
        rendered = recalled.render()
        if rendered:
            parts.append(rendered)
        return "\n\n".join(p for p in parts if p)

    async def _allowed_documents(self) -> list[ContextDocument]:
        """Context documents this agent may read, resolved by the memory gateway."""
        principal = await self._memory().principal_for(self.name, self.domain)
        return list(principal.allowed_documents)

    async def _load_tools(self, task_prompt: str = "") -> list[tuple[Tool, float]]:
        """Return (tool, confidence_score) pairs for this agent, sorted by score descending.

        When a ToolIndex is available and the tool count exceeds SEMANTIC_FILTER_MIN,
        only the top SEMANTIC_TOP_K semantically relevant tools are returned.
        Falls back silently to full injection when the index is unavailable or
        returns no results (e.g. cold start before embeddings are built).
        """
        registry_tools = self._deps.tool_registry.tools_for_agent(self.name)
        scores = dict(await self._deps.confidence_tracker.scores_for_agent(self.name))

        tool_index = self._deps.tool_index
        if task_prompt and tool_index is not None and len(registry_tools) > SEMANTIC_FILTER_MIN:
            top_names = set(await tool_index.search_tools(task_prompt, top_k=SEMANTIC_TOP_K))
            if top_names:
                scored = [(t, scores.get(t.name, 0.5)) for t in registry_tools if t.name in top_names]
                if not scored:
                    scored = [(t, scores.get(t.name, 0.5)) for t in registry_tools]
            else:
                scored = [(t, scores.get(t.name, 0.5)) for t in registry_tools]
        else:
            scored = [(t, scores.get(t.name, 0.5)) for t in registry_tools]

        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored

    def _format_result(self, raw: dict[str, Any]) -> AgentResult:
        """Default: wrap the dict in an `AgentResult`. Override for custom shape."""
        return AgentResult(**raw)
