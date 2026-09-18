"""Global tool registry with filesystem auto-discovery.

Every registered tool is eligible for every agent. Agents receive a lean,
task-relevant subset selected at run time; directory placement is only an
organization detail and never an agent allowlist.

To add a new tool:
  - Drop a .py file with a Tool subclass into any tool package directory
    → it joins the global catalog
  - Tools that need constructor args (e.g. ScheduleTaskTool) are registered manually via
    tool_registry.register() after auto-discovery - they just need to be in specialized/.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import inspect
import logging
import time
from pathlib import Path

from tools.base import Tool
from tools.exceptions import ToolNotFoundError

logger = logging.getLogger(__name__)

_TOOLS_ROOT = Path(__file__).parent
# Package names remain useful for organizing implementations, but they do not
# constrain which agents can use a tool.
_TOOL_DIRS: tuple[tuple[str, str], ...] = (
    ("universal", "tools.universal"),
    ("analysis", "tools.analysis"),
    ("semantic", "tools.semantic"),
    ("specialized", "tools.specialized"),
)
# Hot-reload TTL: re-scan tool directories at most once per this many seconds.
# Short enough that tools written by create_tool mid-task appear quickly;
# long enough that a 12-iteration ReAct loop doesn't scan 12 times.
_RELOAD_TTL_SECONDS = 2.0


def _dir_fingerprint(directory: Path) -> float:
    """A value that changes when any .py file in *directory* is added or edited."""
    return directory.stat().st_mtime + sum(p.stat().st_mtime for p in directory.glob("*.py"))


def _discover(directory: Path, package: str) -> dict[str, Tool]:
    """Scan a directory for Tool subclasses. Returns {tool_name: instance}.

    Files starting with '_' are skipped. Tools that require constructor
    arguments (and therefore raise on bare instantiation) are skipped silently
    - they must be manually registered via ToolRegistry.register().
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
            if isinstance(obj, type) and issubclass(obj, Tool) and obj is not Tool and not inspect.isabstract(obj):
                if _needs_constructor_args(obj):
                    logger.debug(
                        "tool discovery: %s skipped (needs manual registration)",
                        obj.__name__,
                    )
                    continue
                try:
                    instance = obj()
                    tools[instance.name] = instance
                except Exception as exc:
                    # Reached only for a tool that *should* construct bare, so a
                    # TypeError here is a real bug in its __init__, not the
                    # "needs manual registration" case checked above.
                    logger.warning(
                        "tool discovery: %s failed to instantiate: %s",
                        obj.__name__,
                        exc,
                    )
    return tools


def _discover_external(directory: Path) -> dict[str, Tool]:
    """Discover user-owned tools without treating their directory as a package."""
    tools: dict[str, Tool] = {}
    if not directory.exists():
        return tools
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module_name = f"north_learned_tool_{path.stem}_{abs(hash(path.resolve()))}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:
            logger.warning("learned tool discovery: failed to import %s: %s", path, exc)
            continue
        for obj in vars(module).values():
            if isinstance(obj, type) and issubclass(obj, Tool) and obj is not Tool and not inspect.isabstract(obj):
                if _needs_constructor_args(obj):
                    continue
                try:
                    instance = obj()
                    tools[instance.name] = instance
                except Exception as exc:
                    logger.warning("learned tool discovery: failed to construct %s: %s", path, exc)
    return tools


def _needs_constructor_args(tool_cls: type[Tool]) -> bool:
    """True when *tool_cls* cannot be built with no arguments.

    Checked from the signature rather than by catching TypeError, so a genuine
    TypeError raised *inside* a tool's ``__init__`` is reported instead of being
    silently read as "this tool wants injected dependencies".
    """
    try:
        signature = inspect.signature(tool_cls)
    except (TypeError, ValueError):
        return False
    required_kinds = (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )
    return any(
        param.default is inspect.Parameter.empty and param.kind in required_kinds
        for param in signature.parameters.values()
    )


class ToolRegistry:
    """Global catalog of tools available for task-time selection."""

    def __init__(self, auto_register: bool = False, learned_dir: Path | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._learned_dir = learned_dir
        self._last_reload: float = 0.0  # monotonic timestamp of last filesystem scan
        self._dir_mtimes: dict[Path, float] = {}

        if auto_register:
            self._auto_discover()

    def _auto_discover(self) -> None:
        for directory, package in _TOOL_DIRS:
            dir_path = _TOOLS_ROOT / directory
            if dir_path.exists():
                with contextlib.suppress(OSError):
                    self._dir_mtimes[dir_path] = _dir_fingerprint(dir_path)
            for tool in _discover(dir_path, package).values():
                self._tools[tool.name] = tool
        for directory in self._learned_directories():
            if directory.exists():
                with contextlib.suppress(OSError):
                    self._dir_mtimes[directory] = _dir_fingerprint(directory)
            for tool in _discover_external(directory).values():
                self._tools[tool.name] = tool

    def reload(self) -> None:
        """Re-scan tool directories for new or edited files.

        Fast-paths when nothing in a directory has changed. Only adds tools not
        already registered - existing tools are not replaced so in-flight tasks
        are unaffected.
        """
        for directory, package in _TOOL_DIRS:
            dir_path = _TOOLS_ROOT / directory
            if not self._directory_changed(dir_path):
                continue
            for tool in _discover(dir_path, package).values():
                if tool.name not in self._tools:
                    self._tools[tool.name] = tool
                    logger.info("ToolRegistry.reload: picked up new global tool %r", tool.name)
        for directory in self._learned_directories():
            if not self._directory_changed(directory):
                continue
            for tool in _discover_external(directory).values():
                if tool.name not in self._tools:
                    self._tools[tool.name] = tool
                    logger.info("ToolRegistry.reload: picked up learned tool %r", tool.name)

    def _learned_directories(self) -> tuple[Path, ...]:
        if self._learned_dir is None:
            return ()
        return tuple(self._learned_dir / kind for kind in ("universal", "specialized"))

    def _directory_changed(self, dir_path: Path) -> bool:
        """True when *dir_path* has a new/removed/edited .py file since last scan.

        A directory's own mtime only moves when an entry is added or removed, so
        a tool file *edited* in place (create_tool updating an existing tool) was
        never detected. The fingerprint folds in each file's own mtime.
        """
        if not dir_path.exists():
            return False
        try:
            fingerprint = _dir_fingerprint(dir_path)
        except OSError:
            return False
        if self._dir_mtimes.get(dir_path) == fingerprint:
            return False
        self._dir_mtimes[dir_path] = fingerprint
        return True

    def register(self, tool: Tool) -> None:
        """Add a tool to the global catalog."""
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolNotFoundError(f"No tool registered with name: {name}")
        return self._tools[name]

    def all_tools(self) -> list[Tool]:
        """Return every tool in the global catalog."""
        return list(self._tools.values())

    def available_tools(self, *, auto_reload: bool = True) -> list[Tool]:
        """Return every currently available tool in the global catalog.

        Rescans the filesystem at most once per _RELOAD_TTL_SECONDS so new tool
        files written by create_tool mid-task appear within the TTL window without
        scanning on every ReAct iteration.
        Pass auto_reload=False to skip the filesystem scan (useful in tests).

        Relevance is decided from the task by :meth:`agents.base.Agent._load_tools`.
        """
        if auto_reload and (time.monotonic() - self._last_reload) > _RELOAD_TTL_SECONDS:
            self.reload()
            self._last_reload = time.monotonic()
        return self.all_tools()

    def all_tool_names(self) -> set[str]:
        return set(self._tools)

    async def aclose(self) -> None:
        """Call aclose() on every registered tool that defines it."""
        for tool in self._tools.values():
            if hasattr(tool, "aclose") and callable(tool.aclose):
                try:
                    res = tool.aclose()
                    if inspect.isawaitable(res):
                        await res
                except Exception:
                    logger.warning("Error closing tool %s", tool.name, exc_info=True)
