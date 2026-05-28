"""Agent ABC — template method pattern. See docs/CODING_STYLE.md Section 15.1."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from context.models import ContextDocument
from tools.base import Tool

from agents.models import AgentConfig, AgentDependencies, AgentPayload, AgentResult

# Per-document cap before truncation.  ~12k chars ≈ 3k tokens — enough for rich
# personal context without blowing a model's context window on large docs.
_MAX_CONTEXT_CHARS = 12_000


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
        scored_tools = await self._load_tools()
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

    async def _load_context(self, payload: AgentPayload) -> str:
        """Concatenate public.md + judgement_rules.md + relevant past episodes.

        Each context document is capped at _MAX_CONTEXT_CHARS to prevent large
        accumulated documents from overflowing the model's context window.
        """
        if payload.context:
            return payload.context
        store = self._deps.context_store
        raw_parts = [
            await store.read(ContextDocument.PUBLIC),
            await store.read(ContextDocument.JUDGEMENT_RULES),
        ]
        parts = []
        for text in raw_parts:
            if len(text) > _MAX_CONTEXT_CHARS:
                omitted = len(text) - _MAX_CONTEXT_CHARS
                text = text[:_MAX_CONTEXT_CHARS] + f"\n\n[…{omitted} chars omitted — document too large]"
            parts.append(text)

        episodic = self._deps.episodic_store
        if episodic is not None:
            try:
                episodes = await episodic.search(payload.prompt, max_results=3)
                if episodes:
                    block = "## Relevant past context\n" + "\n".join(
                        f"- {e}" for e in episodes
                    )
                    parts.append(block)
            except Exception:
                pass
        return "\n\n".join(p for p in parts if p)

    async def _load_tools(self) -> list[tuple[Tool, float]]:
        """Return (tool, confidence_score) pairs, sorted by score descending.

        Scores are fetched once here and threaded through to `_execute` so no
        subclass needs a second async call to look them up.
        """
        registry_tools = self._deps.tool_registry.tools_for_agent(self.name)
        scores = dict(await self._deps.confidence_tracker.scores_for_agent(self.name))
        scored = [(t, scores.get(t.name, 0.5)) for t in registry_tools]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored

    def _format_result(self, raw: dict[str, Any]) -> AgentResult:
        """Default: wrap the dict in an `AgentResult`. Override for custom shape."""
        return AgentResult(**raw)
