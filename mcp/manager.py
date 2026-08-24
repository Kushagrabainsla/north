"""Manager for discovering MCP configurations and orchestrating client lifecycles."""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from mcp.client import McpClient
from mcp.models import McpConfigFile, McpServerConfig
from tools.specialized.mcp_tool import McpTool

if TYPE_CHECKING:
    from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class McpManager:
    """Discovers MCP server configurations and manages their client instances."""

    def __init__(self, workspace_root: Path | None = None) -> None:
        self.workspace_root = workspace_root
        self._clients: dict[str, McpClient] = {}

    def find_config_files(self) -> list[Path]:
        """Return list of candidate MCP configuration files in priority order."""
        candidates: list[Path] = []
        # 1. Global config in ~/.north/mcp.json (or NORTH_HOME/mcp.json)
        north_home = Path(os.environ.get("NORTH_HOME", "~/.north")).expanduser()
        global_cfg = north_home / "mcp.json"
        if global_cfg.is_file():
            candidates.append(global_cfg)

        # 2. Workspace config in <workspace>/.north/mcp.json or <workspace>/mcp.json
        if self.workspace_root and self.workspace_root.is_dir():
            ws_dot_north = self.workspace_root / ".north" / "mcp.json"
            if ws_dot_north.is_file():
                candidates.append(ws_dot_north)
            ws_mcp = self.workspace_root / "mcp.json"
            if ws_mcp.is_file() and ws_mcp not in candidates:
                candidates.append(ws_mcp)

        return candidates

    def load_configs(self) -> dict[str, McpServerConfig]:
        """Merge server configurations from all found config files."""
        servers: dict[str, McpServerConfig] = {}
        for path in self.find_config_files():
            try:
                data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                cfg = McpConfigFile.model_validate(data)
                for name, srv in cfg.mcp_servers.items():
                    if not srv.disabled:
                        servers[name] = srv
            except Exception:
                logger.warning("Failed to load MCP config from %s", path, exc_info=True)
        return servers

    async def start_all(self) -> None:
        """Start all enabled MCP servers."""
        configs = self.load_configs()
        for name, cfg in configs.items():
            if name in self._clients:
                continue
            client = McpClient(name, cfg)
            try:
                await client.start()
                self._clients[name] = client
                logger.info("Started MCP server %r with %d tools", name, len(client.tools))
            except Exception:
                logger.warning("Failed to start MCP server %r", name, exc_info=True)

    def register_tools(self, registry: ToolRegistry) -> int:
        """Register all tools from active MCP clients into the given ToolRegistry."""
        registered_count = 0
        for name, client in self._clients.items():
            for tool_def in client.tools:
                mcp_tool = McpTool(name, client, tool_def)
                registry.register(mcp_tool)
                registered_count += 1
        return registered_count

    async def close_all(self) -> None:
        """Close all running MCP clients."""
        for client in self._clients.values():
            with contextlib.suppress(Exception):
                await client.close()
        self._clients.clear()
