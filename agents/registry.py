"""Load north, the one agent, from the packaged agents folder."""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

from agents.base import Agent
from agents.constants import NORTH_AGENT
from agents.exceptions import AgentConfigError, AgentNotFoundError
from agents.models import AgentConfig, AgentDependencies
from agents.packaging import agent_module_root

logger = logging.getLogger(__name__)


class AgentRegistry:
    """Loads each packaged agent folder (`config.yaml` and `agent.py`) in `agents_dir`.

    There is one: north. Personal agents are gone; a specialism is a skill on
    north (CODING_STYLE §15). The registry stays the one place an agent is
    looked up by name, so flows, schedules and the API all reach the same one.
    """

    def __init__(self, agents_dir: Path, deps: AgentDependencies) -> None:
        self._deps = deps
        self._agents: dict[str, Agent] = {}
        for entry in sorted(agents_dir.iterdir()) if agents_dir.exists() else []:
            if self._is_valid_agent_directory(entry):
                agent = self._load(entry)
                self._agents.setdefault(agent.name, agent)

    @staticmethod
    def _is_valid_agent_directory(path: Path) -> bool:
        return (
            path.is_dir()
            and not path.name.startswith("_")
            and (path / "config.yaml").exists()
            and (path / "agent.py").exists()
        )

    def _load(self, agent_dir: Path) -> Agent:
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
        return getattr(module, class_name)(config=config, deps=self._deps)

    @property
    def north(self) -> Agent:
        """North, the agent every task, flow step and schedule runs on."""
        return self.get(NORTH_AGENT)

    def get(self, name: str) -> Agent:
        if name not in self._agents:
            raise AgentNotFoundError(f"No agent registered with name: {name}")
        return self._agents[name]

    def all(self) -> list[Agent]:
        return list(self._agents.values())

    def names(self) -> list[str]:
        return list(self._agents.keys())
