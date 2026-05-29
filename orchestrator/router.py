"""Execution Planner / Router (Stage 3).

See docs/CODING_STYLE.md Sections 5.3, 6.5, 9.7, 13.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

from agents import AgentRegistry
from inference import CompletionRequest, InferenceRouter, PoolPriority
from orchestrator.exceptions import RoutingError
from orchestrator.models import ExecutionMode, ExecutionPlan, IntentClassification
from utils.prompts import load_prompt

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
        tool_registry: "ToolRegistry | None" = None,
    ) -> None:
        self._agent_registry = agent_registry
        self._inference_router = inference_router
        self._tool_registry = tool_registry

    async def plan(
        self, prompt: str, classification: IntentClassification, task_id: str
    ) -> ExecutionPlan:
        """Determines execution mode, required agents, and dependency order.

        Args:
            prompt: The user's request prompt.
            classification: The intent classification from Stage 1.
            task_id: The ID of the task being executed.

        Returns:
            The execution plan.

        Raises:
            RoutingError: If plan construction fails and cannot be recovered.
        """
        all_agents = self._agent_registry.all()
        if not all_agents:
            raise RoutingError("No agents are registered. Cannot create execution plan.")

        agents_info = [
            {
                "name": agent.name,
                "domain": agent.domain,
                "accepts": agent.config.accepts,
            }
            for agent in all_agents
        ]

        tools_info = self._summarise_tools()

        try:
            system_prompt = load_prompt("prompts/router.md")
        except Exception as e:
            raise RoutingError(f"Failed to load router prompt template: {e}") from e

        full_prompt = (
            f"{system_prompt}\n\n"
            f"=== Available Agents ===\n{json.dumps(agents_info, indent=2)}\n\n"
            f"=== Available Tools ===\n{json.dumps(tools_info, indent=2)}\n\n"
            f"=== User Request ===\n{prompt}\n\n"
            f"=== Classified Domain ===\n{classification.domain}"
        )

        try:
            response = await self._inference_router.complete(
                CompletionRequest(
                    prompt=full_prompt,
                    priority=PoolPriority.HIGH,
                    component="router",
                    task_id=task_id,
                    json_mode=True,
                )
            )
        except Exception as exc:
            logger.warning("Router LLM call failed — falling back to single %s agent: %s", classification.domain, exc)
            return self._build_fallback_plan(classification.domain, task_id)

        text = response.text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\n", "", text)
            text = re.sub(r"\n```$", "", text)
            text = text.strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.warning("Router LLM response was not valid JSON — falling back to single %s agent: %s", classification.domain, exc)
            return self._build_fallback_plan(classification.domain, task_id)

        return self._build_plan_from_response(data, classification.domain, task_id)

    async def plan_all(
        self, prompt: str, task_id: str
    ) -> tuple[IntentClassification, ExecutionPlan]:
        """Single LLM call that classifies the task AND builds the execution plan.

        Replaces the separate classify → route two-call pipeline.
        """
        all_agents = self._agent_registry.all()
        if not all_agents:
            raise RoutingError("No agents are registered.")

        agents_info = [
            {"name": a.name, "domain": a.domain, "accepts": a.config.accepts}
            for a in all_agents
        ]
        tools_info = self._summarise_tools()

        try:
            system_prompt = load_prompt("prompts/planner.md")
        except Exception as e:
            raise RoutingError(f"Failed to load planner prompt: {e}") from e

        full_prompt = (
            f"{system_prompt}\n\n"
            f"=== Available Agents ===\n{json.dumps(agents_info, indent=2)}\n\n"
            f"=== Available Tools ===\n{json.dumps(tools_info, indent=2)}\n\n"
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
                )
            )
        except Exception as exc:
            logger.warning("Planner LLM call failed — falling back to general single-agent plan: %s", exc)
            return _FALLBACK_CLASSIFICATION, self._build_fallback_plan("general", task_id)

        text = response.text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\n", "", text)
            text = re.sub(r"\n```$", "", text)
            text = text.strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.warning("Planner LLM response was not valid JSON — falling back to general single-agent plan: %s", exc)
            return _FALLBACK_CLASSIFICATION, self._build_fallback_plan("general", task_id)

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
        return classification, plan

    # ------------------------------------------------------------------

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

    def _build_plan_from_response(
        self, data: dict[str, Any], domain: str, task_id: str
    ) -> ExecutionPlan:
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
                return self._build_fallback_plan(domain, task_id)
            # Verify the tool actually exists
            if self._tool_registry is not None:
                try:
                    self._tool_registry.get(direct_tool)
                except Exception:
                    return self._build_fallback_plan(domain, task_id)
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
            return self._build_fallback_plan(domain, task_id)

        registered = set(self._agent_registry.names())
        agents = [a for a in raw_agents if a in registered]
        if not agents:
            return self._build_fallback_plan(domain, task_id)

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
                return self._build_fallback_plan(domain, task_id)

        return ExecutionPlan(
            task_id=task_id,
            agents=agents,
            parallel_groups=parallel_groups,
            dependencies=dependencies,
            mode=mode,
        )

    def _build_fallback_plan(self, domain: str, task_id: str) -> ExecutionPlan:
        """Simple fallback: single agent matching the classified domain."""
        matching = self._agent_registry.for_domain(domain)
        if not matching:
            matching = (
                self._agent_registry.for_domain("general")
                or [self._agent_registry.all()[0]]
            )
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
    def _compute_parallel_groups(
        agents: list[str], dependencies: dict[str, list[str]]
    ) -> list[list[str]]:
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
                raise RoutingError(
                    f"Dependency cycle detected among agents: {sorted(remaining)}"
                )
            groups.append(layer)
            remaining -= set(layer)
        return groups
