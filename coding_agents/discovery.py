"""Which coding agents this machine has."""

from __future__ import annotations

import shutil
from collections.abc import Sequence

from coding_agents.base import CodingBackend
from coding_agents.claude import ClaudeBackend
from coding_agents.codex import CodexBackend


def discover_backends(*, protected_paths: Sequence[str] = ()) -> dict[str, CodingBackend]:
    """The coding agents installed here, by name. An agent that is not installed is simply absent."""
    found: dict[str, CodingBackend] = {}
    if shutil.which("claude"):
        found["claude"] = ClaudeBackend(protected_paths=protected_paths)
    if shutil.which("codex"):
        found["codex"] = CodexBackend(protected_paths=protected_paths)
    return found
