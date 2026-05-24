"""AgentRegistry — discover agents at runtime from the `agents/` filesystem."""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

import yaml

from agents.base import Agent
from agents.exceptions import AgentConfigError, AgentNotFoundError
from agents.models import AgentConfig, AgentDependencies

logger = logging.getLogger(__name__)


class AgentRegistry:
    """Scans `agents_dir` for valid agent folders and constructs each agent.

    A folder is valid if it contains `config.yaml` and `agent.py`. The
    registry imports each agent's module, looks up the class named
    `config.resolved_class_name`, and instantiates it with `(config, deps)`.
    Adding a new agent at runtime is dropping a new folder and restarting.
    """

    def __init__(self, agents_dir: Path, deps: AgentDependencies) -> None:
        self._agents_dir = agents_dir
        self._deps = deps
        self._agents: dict[str, Agent] = {}
        self._discover()

    @classmethod
    def build_tool_graph(cls, agents_dir: Path) -> dict[str, list[str]]:
        """Return {agent_name: [tool_names]} by reading each agent's tools.yaml.

        Scans the same directories that `_discover` would instantiate. Agents
        with no tools.yaml contribute an empty list so the agent still appears
        in the graph (and therefore in `ToolRegistry.agent_names()`).
        Called before constructing `AgentDependencies` so there is no circular
        dependency between AgentRegistry and ToolRegistry.
        """
        graph: dict[str, list[str]] = {}
        if not agents_dir.exists():
            return graph
        for entry in sorted(agents_dir.iterdir()):
            if not cls._is_valid_agent_directory(entry):
                continue
            try:
                config = AgentConfig.from_yaml(entry / "config.yaml")
                graph[config.agent] = cls._load_tool_names(entry)
            except Exception:
                logger.warning("build_tool_graph: skipping %s (failed to load config)", entry.name)
        return graph

    @staticmethod
    def _load_tool_names(agent_dir: Path) -> list[str]:
        """Return the tool names declared in tools.yaml, or [] if the file is absent."""
        tools_yaml = agent_dir / "tools.yaml"
        if not tools_yaml.exists():
            return []
        with tools_yaml.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return []
        tools = data.get("tools", [])
        return [t["name"] for t in tools if isinstance(t, dict) and "name" in t]

    def _discover(self) -> None:
        if not self._agents_dir.exists():
            return
        for entry in sorted(self._agents_dir.iterdir()):
            if self._is_valid_agent_directory(entry):
                agent = self._load_agent_from_directory(entry)
                self._agents[agent.name] = agent

    @staticmethod
    def _is_valid_agent_directory(path: Path) -> bool:
        return (
            path.is_dir()
            and not path.name.startswith("_")
            and (path / "config.yaml").exists()
            and (path / "agent.py").exists()
        )

    def _load_agent_from_directory(self, agent_dir: Path) -> Agent:
        config = AgentConfig.from_yaml(agent_dir / "config.yaml")
        module_path = f"agents.{config.agent}.agent"
        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            raise AgentConfigError(
                f"Failed to import {module_path} for agent '{config.agent}': {e}"
            ) from e

        class_name = config.resolved_class_name
        if not hasattr(module, class_name):
            raise AgentConfigError(
                f"{module_path} is missing class '{class_name}' "
                f"declared in {agent_dir / 'config.yaml'}"
            )
        agent_class = getattr(module, class_name)
        return agent_class(config=config, deps=self._deps)

    def get(self, name: str) -> Agent:
        if name not in self._agents:
            raise AgentNotFoundError(f"No agent registered with name: {name}")
        return self._agents[name]

    def all(self) -> list[Agent]:
        return list(self._agents.values())

    def names(self) -> list[str]:
        return list(self._agents.keys())

    def for_domain(self, domain: str) -> list[Agent]:
        return [a for a in self._agents.values() if a.domain == domain]
