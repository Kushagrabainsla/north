"""Execution Planner / Router (Stage 3).

See docs/CODING_STYLE.md Sections 5.3, 6.5, 9.7, 13.
"""

from __future__ import annotations

import json
import re
from typing import Any

from agents import AgentRegistry
from inference import CompletionRequest, InferenceRouter, PoolPriority
from orchestrator.exceptions import RoutingError
from orchestrator.models import ExecutionPlan, IntentClassification
from utils.prompts import load_prompt


class ExecutionPlanner:
    """Stage 3 orchestrator module that constructs the Agent ExecutionPlan."""

    def __init__(self, agent_registry: AgentRegistry, inference_router: InferenceRouter) -> None:
        self._agent_registry = agent_registry
        self._inference_router = inference_router

    async def plan(
        self, prompt: str, classification: IntentClassification, task_id: str
    ) -> ExecutionPlan:
        """Determines required agents, sequential/parallel groups, and dependencies.

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

        # Serialize available agent profiles
        agents_info = []
        for agent in all_agents:
            agents_info.append(
                {
                    "name": agent.name,
                    "domain": agent.domain,
                    "accepts": agent.config.accepts,
                    "version": agent.config.version,
                }
            )

        agents_str = json.dumps(agents_info, indent=2)
        try:
            system_prompt = load_prompt("prompts/router.md")
        except Exception as e:
            raise RoutingError(f"Failed to load router prompt template: {e}") from e

        full_prompt = (
            f"{system_prompt}\n\n"
            f"=== Available Agents ===\n{agents_str}\n\n"
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
        except Exception as e:
            # Fallback: if inference fails, route to the agent matching the classified domain
            return self._build_fallback_plan(classification.domain, task_id)

        text = response.text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\n", "", text)
            text = re.sub(r"\n```$", "", text)
            text = text.strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return self._build_fallback_plan(classification.domain, task_id)

        # Extract and validate agents
        raw_agents = data.get("agents")
        if not isinstance(raw_agents, list) or not raw_agents:
            return self._build_fallback_plan(classification.domain, task_id)

        # Filter to only keep valid registered agents
        registered_agent_names = set(self._agent_registry.names())
        agents = [a for a in raw_agents if a in registered_agent_names]
        if not agents:
            return self._build_fallback_plan(classification.domain, task_id)

        # Parse dependencies
        raw_deps = data.get("dependencies")
        dependencies: dict[str, list[str]] = {}
        if isinstance(raw_deps, dict):
            for k, v in raw_deps.items():
                if k in agents and isinstance(v, list):
                    dependencies[k] = [dep for dep in v if dep in agents and dep != k]

        # Parse parallel groups or construct dynamically if missing or invalid
        parallel_groups = data.get("parallel_groups")
        if not self._is_valid_parallel_groups(parallel_groups, agents):
            parallel_groups = self._compute_parallel_groups(agents, dependencies)

        return ExecutionPlan(
            task_id=task_id,
            agents=agents,
            parallel_groups=parallel_groups,
            dependencies=dependencies,
        )

    def _build_fallback_plan(self, domain: str, task_id: str) -> ExecutionPlan:
        """Constructs a simple fallback plan targeting the domain's primary agent."""
        matching_agents = self._agent_registry.for_domain(domain)
        if not matching_agents:
            # Prefer the general agent for unmatched domains; fall back to first registered
            matching_agents = (
                self._agent_registry.for_domain("general")
                or [self._agent_registry.all()[0]]
            )

        agent_name = matching_agents[0].name
        return ExecutionPlan(
            task_id=task_id,
            agents=[agent_name],
            parallel_groups=[[agent_name]],
            dependencies={},
        )

    @staticmethod
    def _is_valid_parallel_groups(groups: Any, agents: list[str]) -> bool:
        """Checks if the provided groups structure lists all planned agents."""
        if not isinstance(groups, list) or not groups:
            return False
        flat_list = []
        for g in groups:
            if not isinstance(g, list):
                return False
            for agent in g:
                if not isinstance(agent, str):
                    return False
                flat_list.append(agent)

        return set(flat_list) == set(agents)

    @staticmethod
    def _compute_parallel_groups(
        agents: list[str], dependencies: dict[str, list[str]]
    ) -> list[list[str]]:
        """Calculates correct execution steps using a layer-based topological sort."""
        # Work on a copy of dependencies and agents list
        remaining = set(agents)
        deps = {agent: set(dependencies.get(agent, [])) for agent in agents}

        groups: list[list[str]] = []
        while remaining:
            # Find agents in 'remaining' with no dependencies in 'remaining'
            current_layer = []
            for agent in sorted(remaining):
                # If dependencies of this agent intersect with what's remaining, it cannot run yet
                if not (deps[agent] & remaining):
                    current_layer.append(agent)

            if not current_layer:
                # Circular dependency detected, break cycle by dumping rest into a final layer
                groups.append(sorted(list(remaining)))
                break

            groups.append(current_layer)
            remaining -= set(current_layer)

        return groups
