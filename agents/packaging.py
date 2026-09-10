"""Resolution of the importable package that owns agent implementations.

Derived from this package rather than hardcoded, so scaffolded agents keep
importing correctly if the project's packages are ever relocated.
"""

from __future__ import annotations


def agent_module_root() -> str:
    """Return the dotted package name that contains per-agent modules."""
    return __package__ or "agents"
