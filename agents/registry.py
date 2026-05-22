"""AgentRegistry — discover agents at runtime from the `agents/` filesystem."""

from __future__ import annotations

import importlib
from pathlib import Path

from agents.base import Agent
from agents.exceptions import AgentConfigError, AgentNotFoundError
from agents.models import AgentConfig, AgentDependencies


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
