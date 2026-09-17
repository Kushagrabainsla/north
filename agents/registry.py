"""Discover packaged built-in agents and user-authored agents."""

from __future__ import annotations

import importlib
import importlib.util
import logging
import shutil
import sys
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

    def __init__(
        self,
        agents_dir: Path,
        deps: AgentDependencies,
        user_agents_dir: Path | None = None,
    ) -> None:
        self._agents_dir = agents_dir
        self._user_agents_dir = user_agents_dir
        self._deps = deps
        self._agents: dict[str, Agent] = {}
        self._agent_directories: dict[str, tuple[Path, bool]] = {}
        self._discover()

    def _discover(self) -> None:
        for root, is_user in self._roots():
            if not root.exists():
                continue
            for entry in sorted(root.iterdir()):
                if not self._is_valid_agent_directory(entry):
                    continue
                if is_user:
                    # Read the cheap, declarative identity first. Besides avoiding
                    # needless imports, this prevents a colliding personal module
                    # from executing at all when a packaged agent owns the name.
                    try:
                        config = AgentConfig.from_yaml(entry / "config.yaml")
                    except Exception as exc:
                        logger.warning("AgentRegistry: skipping %s - invalid config.yaml: %s", entry.name, exc)
                        continue
                    if config.agent in self._agents:
                        continue
                    try:
                        agent = self._load_agent_from_directory(entry, is_user=True)
                    except Exception as exc:
                        # A broken optional extension must not stop North from
                        # starting. Packaged-agent failures still fail fast below.
                        logger.warning("AgentRegistry: failed to load personal agent %s: %s", entry.name, exc)
                        continue
                else:
                    agent = self._load_agent_from_directory(entry, is_user=False)

                # Packaged agents are authoritative if a personal extension uses
                # the same name. This also makes updates deterministic.
                if agent.name not in self._agents:
                    self._agents[agent.name] = agent
                    self._agent_directories[agent.name] = (entry, is_user)

    def _roots(self) -> list[tuple[Path, bool]]:
        roots = [(self._agents_dir, False)]
        if self._user_agents_dir is not None:
            roots.append((self._user_agents_dir, True))
        return roots

    @staticmethod
    def _is_valid_agent_directory(path: Path) -> bool:
        return (
            path.is_dir()
            and not path.name.startswith("_")
            and (path / "config.yaml").exists()
            and (path / "agent.py").exists()
        )

    def _load_agent_from_directory(self, agent_dir: Path, *, is_user: bool = False) -> Agent:
        config = AgentConfig.from_yaml(agent_dir / "config.yaml")
        if is_user:
            module_path = f"north_user_agents.{config.agent}.agent"
            agent_file = agent_dir / "agent.py"
            spec = importlib.util.spec_from_file_location(
                module_path,
                agent_file,
                submodule_search_locations=[str(agent_dir)],
            )
            if spec is None or spec.loader is None:
                raise AgentConfigError(f"Failed to load {agent_file} for agent '{config.agent}'")
            module = importlib.util.module_from_spec(spec)
            # LLMAgent resolves its prompt relative to the loaded module via
            # sys.modules, just like a normally imported built-in agent.
            sys.modules[module_path] = module
            try:
                spec.loader.exec_module(module)
            except Exception as e:
                # Do not leave a half-initialised module that a later reload may
                # mistake for a successfully imported personal agent.
                sys.modules.pop(module_path, None)
                raise AgentConfigError(f"Failed to import {agent_file} for agent '{config.agent}': {e}") from e
        else:
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
        for root, is_user in self._roots():
            if not root.exists():
                continue
            for entry in sorted(root.iterdir()):
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
                    agent = self._load_agent_from_directory(entry, is_user=is_user)
                    self._agents[agent.name] = agent
                    self._agent_directories[agent.name] = (entry, is_user)
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

    def source_of(self, name: str) -> str:
        """Return ``builtin`` or ``personal`` for a registered agent."""
        source = self._agent_directories.get(name)
        return "personal" if source and source[1] else "builtin"

    def remove_personal(self, name: str) -> bool:
        """Remove a personal agent directory; built-ins can never be removed."""
        entry = self._agent_directories.get(name)
        if entry is None or not entry[1] or self._user_agents_dir is None:
            return False
        agent_dir = entry[0].resolve()
        user_root = self._user_agents_dir.resolve()
        if agent_dir.parent != user_root:
            return False
        shutil.rmtree(agent_dir)
        self._agents.pop(name, None)
        self._agent_directories.pop(name, None)
        return True
