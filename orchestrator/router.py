"""Execution Planner / Router (Stage 3).

See docs/CODING_STYLE.md Sections 5.3, 6.5, 9.7, 13.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from agents import AgentRegistry
from inference import CompletionRequest, InferenceRouter, PoolPriority
from orchestrator.exceptions import RoutingError
from orchestrator.models import ExecutionMode, ExecutionPlan, IntentClassification
from utils.prompts import load_prompt
from utils.text import strip_code_fences

_PLAN_CACHE_TTL_SECONDS: int = 3600  # 1 hour
_PLAN_CACHE_MAX_SIZE: int = 256
_NORMALIZE_RE = re.compile(r"[^a-z0-9 ]")
# Only the most recent dialogue disambiguates a follow-up; cap it so the planner
# prompt and the routing cache key stay bounded on long conversations.
_PLANNER_CONVERSATION_TAIL_CHARS: int = 2000


def _normalize(text: str) -> str:
    return " ".join(_NORMALIZE_RE.sub("", text.lower().strip()).split())


def _plan_cache_key(prompt: str, conversation: str = "") -> str:
    """Stable hash of the normalized prompt (plus recent conversation) for routing.

    The conversation is folded in because it now affects routing — the same
    prompt ("yes, go ahead") must not reuse a plan cached under a different
    conversation, or follow-ups would route to the wrong agent.
    """
    normalized = _normalize(prompt)
    if conversation:
        normalized = f"{normalized}␟{_normalize(conversation)}"
    return hashlib.md5(normalized.encode()).hexdigest()


def _recent_conversation(context: str) -> str:
    """Extract just the recent-conversation section from a context blob.

    The planner only needs dialogue to disambiguate follow-ups like "go ahead";
    personal background facts add noise and would pollute the routing cache key.
    Returns "" when the blob has no conversation section (backward compatible).
    """
    if context.startswith("## Recent conversation"):
        # Keep up to the next top-level (##) section, matching how the agent
        # splits the same blob in _build_task_message.
        parts = re.split(r"\n\n(?=##)", context, maxsplit=1)
        section = parts[0].strip().removeprefix("## Recent conversation").strip()
        return section[-_PLANNER_CONVERSATION_TAIL_CHARS:]
    return ""


logger = logging.getLogger(__name__)

_FALLBACK_CLASSIFICATION = IntentClassification(
    is_consequential=False, domain="general", reasoning="planner fallback", confidence=1.0
)

if TYPE_CHECKING:
    from tools.registry import ToolRegistry


class ExecutionPlanner:
    """Stage 3 orchestrator module that constructs the Agent ExecutionPlan."""

    def __init__(
        self,
        agent_registry: AgentRegistry,
        inference_router: InferenceRouter,
        tool_registry: ToolRegistry | None = None,
        workspace: str = "",
    ) -> None:
        self._agent_registry = agent_registry
        self._inference_router = inference_router
        self._tool_registry = tool_registry
        self._workspace = workspace
        # Cache: normalized_hash → (insert_ts, classification, plan)
        self._plan_cache: dict[str, tuple[float, IntentClassification, ExecutionPlan]] = {}

    async def plan_all(
        self, prompt: str, task_id: str, context: str = ""
    ) -> tuple[IntentClassification, ExecutionPlan]:
        """Single LLM call that classifies the task AND builds the execution plan.

        Replaces the separate classify → route two-call pipeline. *context* is the
        task's context blob; its recent-conversation section is given to the
        planner so follow-ups ("yes, go ahead") route from the dialogue, not blind.
        """
        all_agents = self._agent_registry.all()
        if not all_agents:
            raise RoutingError("No agents are registered.")

        conversation = _recent_conversation(context)
        cache_key = _plan_cache_key(prompt, conversation)
        cached = self._plan_cache.get(cache_key)
        if cached is not None:
            insert_ts, cached_cls, cached_plan = cached
            # Revalidate against the current registries: agents/tools can be
            # created or removed at runtime, and executing a stale plan fails
            # only at agent-lookup time, well after planning.
            if (time.monotonic() - insert_ts) < _PLAN_CACHE_TTL_SECONDS and self._plan_still_valid(cached_plan):
                logger.debug("Planner cache hit for key %s", cache_key[:8])
                # Return a fresh plan with the new task_id so task tracking is correct.
                return cached_cls, cached_plan.with_task_id(task_id)
            del self._plan_cache[cache_key]

        agents_info = [{"name": a.name, "domain": a.domain, "accepts": a.config.accepts} for a in all_agents]
        tools_info = self._summarise_tools()

        try:
            system_prompt = load_prompt("prompts/planner.md")
        except Exception as e:
            raise RoutingError(f"Failed to load planner prompt: {e}") from e

        system_context_lines = []
        if self._workspace:
            system_context_lines.append(f"- workspace (default cwd for shell/file tools): {self._workspace}")
            system_context_lines.append(
                "- When constructing filesystem paths, always prefer absolute paths derived from the "
                "workspace above. Never emit bare filenames or paths starting with '~' — expand them."
            )
        system_context_block = (
            "=== System Context ===\n" + "\n".join(system_context_lines) + "\n\n" if system_context_lines else ""
        )

        conversation_block = (
            f"=== Recent Conversation ===\n{conversation}\n"
            "(Use this to resolve what the User Task refers to — e.g. a short "
            "confirmation like 'yes, go ahead' continues the work just discussed. "
            "Classify and route based on that actual intent.)\n\n"
            if conversation
            else ""
        )

        full_prompt = (
            f"{system_prompt}\n\n"
            f"{system_context_block}"
            f"=== Available Agents ===\n{json.dumps(agents_info, indent=2)}\n\n"
            f"=== Available Tools ===\n{json.dumps(tools_info, indent=2)}\n\n"
            f"{conversation_block}"
            f"=== User Task ===\n{prompt}"
        )

        try:
            response = await self._inference_router.complete(
                CompletionRequest(
                    prompt=full_prompt,
                    priority=PoolPriority.HIGH,
                    component="planner",
                    task_id=task_id,
                    json_mode=True,
                    temperature=0.0,
                )
            )
        except Exception as exc:
            logger.warning("Planner LLM call failed — falling back to general single-agent plan: %s", exc)
            return _FALLBACK_CLASSIFICATION, self.build_fallback_plan("general", task_id)

        try:
            data = json.loads(strip_code_fences(response.text))
        except json.JSONDecodeError as exc:
            logger.warning(
                "Planner LLM response was not valid JSON — falling back to general single-agent plan: %s",
                exc,
            )
            return _FALLBACK_CLASSIFICATION, self.build_fallback_plan("general", task_id)

        raw_confidence = data.get("confidence", 0.9)
        try:
            confidence = max(0.0, min(1.0, float(raw_confidence)))
        except (TypeError, ValueError):
            confidence = 0.9
        classification = IntentClassification(
            is_consequential=bool(data.get("is_consequential", False)),
            domain=str(data.get("domain", "general")),
            reasoning=str(data.get("reasoning", "")),
            confidence=confidence,
        )
        plan = self._build_plan_from_response(data, classification.domain, task_id)

        # Evict oldest entries when cache is full, then store.
        if len(self._plan_cache) >= _PLAN_CACHE_MAX_SIZE:
            oldest = min(self._plan_cache, key=lambda k: self._plan_cache[k][0])
            del self._plan_cache[oldest]
        self._plan_cache[cache_key] = (time.monotonic(), classification, plan)

        return classification, plan

    # ------------------------------------------------------------------

    def _plan_still_valid(self, plan: ExecutionPlan) -> bool:
        """Return True when every agent/tool the cached plan references still exists."""
        if plan.mode == ExecutionMode.SINGLE_TOOL:
            if self._tool_registry is None or not plan.direct_tool:
                return False
            try:
                self._tool_registry.get(plan.direct_tool)
            except Exception:
                return False
            return True
        registered = set(self._agent_registry.names())
        return bool(plan.agents) and set(plan.agents) <= registered

    def _summarise_tools(self) -> list[dict[str, Any]]:
        """Return a compact list of available tools for the router prompt."""
        if self._tool_registry is None:
            return []
        summaries = []
        for name in sorted(self._tool_registry.all_tool_names()):
            try:
                tool = self._tool_registry.get(name)
                schema = tool.parameters_schema
                required = schema.get("required", [])
                props = schema.get("properties", {})
                params = []
                for param_name, param_def in props.items():
                    req = "required" if param_name in required else "optional"
                    desc = param_def.get("description", "")
                    params.append(f"{param_name} ({req}): {desc}")
                summaries.append({"name": name, "description": tool.description, "params": params})
            except Exception:
                continue
        return summaries

    def _build_plan_from_response(self, data: dict[str, Any], domain: str, task_id: str) -> ExecutionPlan:
        """Parse the router LLM response into an ExecutionPlan."""
        raw_mode = data.get("mode", "single_agent")
        try:
            mode = ExecutionMode(raw_mode)
        except ValueError:
            mode = ExecutionMode.SINGLE_AGENT

        # single_tool path
        if mode == ExecutionMode.SINGLE_TOOL:
            direct_tool = data.get("direct_tool")
            direct_tool_params = data.get("direct_tool_params") or {}
            if not direct_tool or not isinstance(direct_tool_params, dict):
                return self.build_fallback_plan(domain, task_id)
            # Verify the tool actually exists
            if self._tool_registry is not None:
                try:
                    self._tool_registry.get(direct_tool)
                except Exception:
                    return self.build_fallback_plan(domain, task_id)
            return ExecutionPlan(
                task_id=task_id,
                agents=[],
                parallel_groups=[],
                dependencies={},
                mode=ExecutionMode.SINGLE_TOOL,
                direct_tool=direct_tool,
                direct_tool_params=direct_tool_params,
            )

        # agent-based paths
        raw_agents = data.get("agents")
        if not isinstance(raw_agents, list) or not raw_agents:
            return self.build_fallback_plan(domain, task_id)

        registered = set(self._agent_registry.names())
        agents = [a for a in raw_agents if a in registered]
        if not agents:
            return self.build_fallback_plan(domain, task_id)

        raw_deps = data.get("dependencies")
        dependencies: dict[str, list[str]] = {}
        if isinstance(raw_deps, dict):
            for k, v in raw_deps.items():
                if k in agents and isinstance(v, list):
                    dependencies[k] = [dep for dep in v if dep in agents and dep != k]

        parallel_groups = data.get("parallel_groups")
        if not self._is_valid_parallel_groups(parallel_groups, agents):
            try:
                parallel_groups = self._compute_parallel_groups(agents, dependencies)
            except RoutingError:
                logger.warning(
                    "Dependency cycle in LLM response for agents %s — falling back to single agent",
                    agents,
                )
                return self.build_fallback_plan(domain, task_id)

        return ExecutionPlan(
            task_id=task_id,
            agents=agents,
            parallel_groups=parallel_groups,
            dependencies=dependencies,
            mode=mode,
        )

    def build_fallback_plan(self, domain: str, task_id: str) -> ExecutionPlan:
        """Simple fallback: single agent matching the classified domain."""
        matching = self._agent_registry.for_domain(domain)
        if not matching:
            matching = self._agent_registry.for_domain("general") or [self._agent_registry.all()[0]]
        name = matching[0].name
        return ExecutionPlan(
            task_id=task_id,
            agents=[name],
            parallel_groups=[[name]],
            dependencies={},
            mode=ExecutionMode.SINGLE_AGENT,
        )

    @staticmethod
    def _is_valid_parallel_groups(groups: Any, agents: list[str]) -> bool:
        if not isinstance(groups, list) or not groups:
            return False
        flat: list[str] = []
        for g in groups:
            if not isinstance(g, list):
                return False
            for a in g:
                if not isinstance(a, str):
                    return False
                flat.append(a)
        return set(flat) == set(agents)

    @staticmethod
    def _compute_parallel_groups(agents: list[str], dependencies: dict[str, list[str]]) -> list[list[str]]:
        """Layer-based topological sort to compute parallel execution groups.

        Raises RoutingError if a dependency cycle is detected so callers can
        fall back to a safe single-agent plan rather than silently running
        dependent agents in parallel.
        """
        remaining = set(agents)
        deps = {a: set(dependencies.get(a, [])) for a in agents}
        groups: list[list[str]] = []
        while remaining:
            layer = [a for a in sorted(remaining) if not (deps[a] & remaining)]
            if not layer:
                raise RoutingError(f"Dependency cycle detected among agents: {sorted(remaining)}")
            groups.append(layer)
            remaining -= set(layer)
        return groups
