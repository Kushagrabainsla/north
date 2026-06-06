"""Tool registry with auto-discovery from tools/universal/ and tools/specialized/.

Universal tools are given to every agent automatically.
Specialized tools are available to agents that declare them in tools.yaml.

To add a new tool:
  - Drop a .py file with a Tool subclass into tools/universal/ → all agents get it
  - Drop a .py file with a Tool subclass into tools/specialized/ → agents opt in via tools.yaml
  - Tools that need constructor args (e.g. ScheduleTaskTool) are registered manually via
    tool_registry.register() after auto-discovery — they just need to be in specialized/.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import time
from pathlib import Path

from tools.base import Tool
from tools.exceptions import ToolNotFoundError

logger = logging.getLogger(__name__)

_TOOLS_ROOT = Path(__file__).parent
# Hot-reload TTL: re-scan tool directories at most once per this many seconds.
# Short enough that tools written by create_tool mid-task appear quickly;
# long enough that a 12-iteration ReAct loop doesn't scan 12 times.
_RELOAD_TTL_SECONDS = 2.0


def _discover(directory: Path, package: str) -> dict[str, Tool]:
    """Scan a directory for Tool subclasses. Returns {tool_name: instance}.

    Files starting with '_' are skipped. Tools that require constructor
    arguments (and therefore raise on bare instantiation) are skipped silently
    — they must be manually registered via ToolRegistry.register().
    """
    tools: dict[str, Tool] = {}
    if not directory.exists():
        return tools
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module_name = f"{package}.{path.stem}"
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            logger.warning("tool discovery: failed to import %s: %s", module_name, exc)
            continue
        for obj in vars(module).values():
            if (
                isinstance(obj, type)
                and issubclass(obj, Tool)
                and obj is not Tool
                and not inspect.isabstract(obj)
            ):
                try:
                    instance = obj()
                    tools[instance.name] = instance
                except TypeError:
                    logger.debug(
                        "tool discovery: %s skipped (needs manual registration)",
                        obj.__name__,
                    )
                except Exception as exc:
                    logger.warning(
                        "tool discovery: %s failed to instantiate: %s",
                        obj.__name__, exc,
                    )
    return tools


class ToolRegistry:
    """Registry of Tool instances split into universal and specialized.

    Universal tools are returned for every agent.
    Specialized tools are returned only for agents that declare them in their
    tool graph (built from tools.yaml).

    New tool files dropped into tools/universal/ or tools/specialized/ at
    runtime are picked up automatically the first time any agent tries to call
    them — no restart required.
    """

    def __init__(
        self,
        graph: dict[str, list[str]] | None = None,
        auto_register: bool = False,
    ) -> None:
        self._graph: dict[str, list[str]] = graph or {}
        self._tools: dict[str, Tool] = {}
        self._universal: list[str] = []
        self._last_reload: float = 0.0  # monotonic timestamp of last filesystem scan

        if auto_register:
            self._auto_discover()

    def _auto_discover(self) -> None:
        universal = _discover(_TOOLS_ROOT / "universal", "tools.universal")
        for tool in universal.values():
            self._tools[tool.name] = tool
        self._universal = list(universal.keys())

        specialized = _discover(_TOOLS_ROOT / "specialized", "tools.specialized")
        for tool in specialized.values():
            self._tools[tool.name] = tool

    def reload(self) -> None:
        """Re-scan tool directories for new files.

        Only adds tools not already registered — existing tools are not
        replaced so in-flight tasks are unaffected.
        """
        universal = _discover(_TOOLS_ROOT / "universal", "tools.universal")
        for tool in universal.values():
            if tool.name not in self._tools:
                self._tools[tool.name] = tool
                if tool.name not in self._universal:
                    self._universal.append(tool.name)
                logger.info("ToolRegistry.reload: picked up new universal tool %r", tool.name)

        specialized = _discover(_TOOLS_ROOT / "specialized", "tools.specialized")
        for tool in specialized.values():
            if tool.name not in self._tools:
                self._tools[tool.name] = tool
                logger.info("ToolRegistry.reload: picked up new specialized tool %r", tool.name)

    def update_graph(self, agent_name: str, tool_names: list[str]) -> None:
        """Add or extend the specialized tool list for an agent.

        Called when a new agent is discovered at runtime so its tools.yaml
        bindings are honoured without a restart.
        """
        existing = self._graph.get(agent_name, [])
        merged = list(existing)
        for name in tool_names:
            if name not in merged:
                merged.append(name)
        self._graph[agent_name] = merged

    def register(self, tool: Tool) -> None:
        """Manually register a tool (e.g. one that needs constructor args)."""
        self._tools[tool.name] = tool

    def make_universal(self, name: str) -> None:
        """Mark a manually registered tool as universal (given to all agents).

        Use this for tools that live in tools/universal/ but require constructor
        args and therefore can't be auto-instantiated during discovery.
        """
        if name not in self._universal:
            self._universal.append(name)

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolNotFoundError(f"No tool registered with name: {name}")
        return self._tools[name]

    def tools_for_agent(self, agent: str, *, auto_reload: bool = True) -> list[Tool]:
        """Return universal tools + any specialized tools the agent declared.

        Rescans the filesystem at most once per _RELOAD_TTL_SECONDS so new tool
        files written by create_tool mid-task appear within the TTL window without
        scanning on every ReAct iteration.
        Pass auto_reload=False to skip the filesystem scan (useful in tests).
        """
        if auto_reload and (time.monotonic() - self._last_reload) > _RELOAD_TTL_SECONDS:
            self.reload()
            self._last_reload = time.monotonic()
        result: list[Tool] = []
        seen: set[str] = set()

        for name in self._universal:
            if name in self._tools:
                result.append(self._tools[name])
                seen.add(name)

        for name in self._graph.get(agent, []):
            if name in self._tools and name not in seen:
                result.append(self._tools[name])
                seen.add(name)

        return result

    def agent_names(self) -> list[str]:
        return list(self._graph.keys())

    def all_tool_names(self) -> set[str]:
        specialized = {name for names in self._graph.values() for name in names}
        return set(self._universal) | specialized
