"""AgentRegistry - discover agents at runtime from the `agents/` filesystem."""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

from agents.base import Agent
from agents.exceptions import AgentConfigError, AgentNotFoundError
from agents.models import AgentConfig, AgentDependencies
from agents.packaging import agent_module_root

logger = logging.getLogger(__name__)

# Backward-compatibility aliases: map legacy agent names to their current name so
# that delegations, plans, or LLM output using an old name still resolve. The
# tester agent was broadened into the reviewer (QA + code review) in v1.4.
_AGENT_ALIASES: dict[str, str] = {"tester": "reviewer"}


class AgentRegistry:
    """Scans `agents_dir` for valid agent folders and constructs each agent.

    A folder is valid if it contains `config.yaml` and `agent.py`. The
    registry imports each agent's module, looks up the class named
    `config.resolved_class_name`, and instantiates it with `(config, deps)`.
    New agent folders dropped at runtime are picked up automatically on the
    next call to `get()` - no restart required.
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
        module_path = f"{agent_module_root()}.{config.agent}.agent"
        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            raise AgentConfigError(f"Failed to import {module_path} for agent '{config.agent}': {e}") from e

        class_name = config.resolved_class_name
        if not hasattr(module, class_name):
            raise AgentConfigError(
                f"{module_path} is missing class '{class_name}' declared in {agent_dir / 'config.yaml'}"
            )
        agent_class = getattr(module, class_name)
        return agent_class(config=config, deps=self._deps)

    def reload(self) -> list[str]:
        """Discover agent directories added since startup.

        Existing agents are not re-instantiated - only new folders are loaded.
        Returns the names of newly registered agents.
        """
        new_names: list[str] = []
        if not self._agents_dir.exists():
            return new_names
        for entry in sorted(self._agents_dir.iterdir()):
            if not self._is_valid_agent_directory(entry):
                continue
            try:
                config = AgentConfig.from_yaml(entry / "config.yaml")
            except Exception as exc:
                logger.warning("AgentRegistry.reload: skipping %s - invalid config.yaml: %s", entry.name, exc)
                continue
            if config.agent in self._agents:
                continue
            try:
                agent = self._load_agent_from_directory(entry)
                self._agents[agent.name] = agent
                new_names.append(agent.name)
                logger.info("AgentRegistry.reload: picked up new agent %r", agent.name)
            except Exception as exc:
                logger.warning("AgentRegistry.reload: failed to load %s: %s", entry.name, exc)
        return new_names

    def get(self, name: str) -> Agent:
        name = _AGENT_ALIASES.get(name, name)
        if name not in self._agents:
            # Trigger a live filesystem scan before raising - new agent folders
            # dropped at runtime are registered here without a server restart.
            self.reload()
        if name not in self._agents:
            raise AgentNotFoundError(f"No agent registered with name: {name}")
        return self._agents[name]

    def all(self) -> list[Agent]:
        return list(self._agents.values())

    def names(self) -> list[str]:
        return list(self._agents.keys())

    def for_domain(self, domain: str) -> list[Agent]:
        return [a for a in self._agents.values() if a.domain == domain]
