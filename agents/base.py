"""Agent ABC — template method pattern. See docs/CODING_STYLE.md Section 15.1."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from context.models import ContextDocument
from tools.base import Tool

from agents.models import AgentConfig, AgentDependencies, AgentPayload, AgentResult


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
        tools = await self._load_tools()
        raw = await self._execute(payload, context, tools)
        return self._format_result(raw)

    @abstractmethod
    async def _execute(
        self,
        payload: AgentPayload,
        context: str,
        tools: list[Tool],
    ) -> dict[str, Any]:
        """Domain-specific logic. Returns a dict that maps onto `AgentResult` fields."""

    async def _load_context(self, payload: AgentPayload) -> str:
        """Default: concatenate `public.md` and `judgement_rules.md`.

        Subclasses can override to read additional documents or to pull
        from `payload.context` instead of disk.
        """
        if payload.context:
            return payload.context
        store = self._deps.context_store
        parts = [
            await store.read(ContextDocument.PUBLIC),
            await store.read(ContextDocument.JUDGEMENT_RULES),
        ]
        return "\n\n".join(p for p in parts if p)

    async def _load_tools(self) -> list[Tool]:
        """Default: load the agent's tools, sorted by confidence descending."""
        registry_tools = self._deps.tool_registry.tools_for_agent(self.name)
        scores = dict(await self._deps.confidence_tracker.scores_for_agent(self.name))
        registry_tools.sort(
            key=lambda t: scores.get(t.name, 0.5),  # default score for unseen tools
            reverse=True,
        )
        return registry_tools

    def _format_result(self, raw: dict[str, Any]) -> AgentResult:
        """Default: wrap the dict in an `AgentResult`. Override for custom shape."""
        return AgentResult(**raw)
