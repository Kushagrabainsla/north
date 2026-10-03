"""The environment a coding agent starts with."""

from __future__ import annotations

import os
from collections.abc import Mapping

from coding_agents.constants import INHERITED_ENVIRONMENT


def agent_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """*source* narrowed to what a worker needs; north's secrets and other providers' keys stay behind.

    The one place a worker's environment is read: the process boundary, not a setting.
    """
    current = os.environ if source is None else source
    return {name: current[name] for name in INHERITED_ENVIRONMENT if name in current}
