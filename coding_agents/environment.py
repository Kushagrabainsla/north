"""The environment a coding agent starts with."""

from __future__ import annotations

import os
from collections.abc import Mapping

from coding_agents.constants import INHERITED_ENVIRONMENT, NETWORK_ENVIRONMENT


def agent_environment(source: Mapping[str, str] | None = None, *, network: bool = False) -> dict[str, str]:
    """*source* narrowed to what a worker needs; north's secrets and other providers' keys stay behind.

    The one place a child process's environment is read: the process boundary, not
    a setting. *network* adds the proxy and certificate settings a child that calls
    a web API needs, such as an MCP server.
    """
    current = os.environ if source is None else source
    names = INHERITED_ENVIRONMENT + (NETWORK_ENVIRONMENT if network else ())
    return {name: current[name] for name in names if name in current}
