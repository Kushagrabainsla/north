"""Agent ABC — template method pattern. See docs/CODING_STYLE.md Section 15.1."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from agents.models import AgentConfig, AgentDependencies, AgentPayload, AgentResult
from context.models import ContextDocument
from tools.base import Tool
from tools.tool_index import SEMANTIC_FILTER_MIN, SEMANTIC_TOP_K

logger = logging.getLogger(__name__)

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

    async def _load_context(self, payload: AgentPayload) -> str:
        """Load context for this agent.

        Preference order:
        1. payload.context (pre-loaded by orchestrator for delegated tasks)
        2. FactStore semantic search (when available and populated)
        3. Full markdown document load (legacy fallback)

        Episodic search runs in all paths and is appended at the end.
        """
        if payload.context:
            return payload.context

        parts: list[str] = []

        fact_store = self._deps.fact_store
        if fact_store is not None:
            try:
                if await fact_store.count() > 0:
                    facts = await fact_store.search(payload.prompt, max_results=15)
                    if facts:
                        parts.append(
                            "## Personal Context\n"
                            + "\n".join(f"- {f}" for f in facts)
                        )
            except Exception as exc:
                logger.warning("FactStore search failed for task %s: %s", payload.task_id, exc)

        if not parts:
            store = self._deps.context_store
            allowed_docs = await self._allowed_documents()
            raw_parts = [await store.read(doc) for doc in allowed_docs]
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
                    parts.append(
                        "## Relevant past context\n"
                        + "\n".join(f"- {e}" for e in episodes)
                    )
            except Exception as exc:
                logger.warning("Episodic context search failed for task %s: %s", payload.task_id, exc)
        return "\n\n".join(p for p in parts if p)

    async def _allowed_documents(self) -> list[ContextDocument]:
        """Return the context documents this agent is permitted to read.

        Parses lines of the form ``<agent>: can_read: <doc>, <doc>`` from
        privacy_rules.md.  Falls back to [PUBLIC, JUDGEMENT_RULES] when the
        file is missing, empty, or contains no rule for this agent.

        Engineering agents also read north_stars by default — design and
        implementation decisions should be checked against long-term goals.

        Example privacy_rules.md line:
            health: can_read: public.md, judgement_rules.md
        """
        _DEFAULT = (
            [ContextDocument.PUBLIC, ContextDocument.JUDGEMENT_RULES, ContextDocument.NORTH_STARS]
            if self.domain == "engineering"
            else [ContextDocument.PUBLIC, ContextDocument.JUDGEMENT_RULES]
        )
        try:
            rules_text = await self._deps.context_store.read(ContextDocument.PRIVACY_RULES)
        except Exception:
            return _DEFAULT
        if not rules_text.strip():
            return _DEFAULT
        for line in rules_text.splitlines():
            line = line.strip()
            if not line.startswith(f"{self.name}:") or "can_read:" not in line:
                continue
            after = line.split("can_read:", 1)[1]
            docs: list[ContextDocument] = []
            for token in after.split(","):
                token = token.strip()
                try:
                    docs.append(ContextDocument(token))
                except ValueError:
                    logger.debug(
                        "privacy_rules.md: unknown document %r for agent %s — skipping",
                        token,
                        self.name,
                    )
            return docs if docs else _DEFAULT
        return _DEFAULT

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
        if (
            task_prompt
            and tool_index is not None
            and len(registry_tools) > SEMANTIC_FILTER_MIN
        ):
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
