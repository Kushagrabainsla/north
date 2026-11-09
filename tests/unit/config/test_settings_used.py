"""A setting north offers must be one north reads: `inference.pool_refresh_hours` was offered and did nothing."""

from __future__ import annotations

import re
from pathlib import Path

from config.settings import Settings

_ROOT = Path(__file__).resolve().parents[3]
_SOURCE_DIRS = (
    "agents",
    "approval",
    "bootstrap",
    "cli",
    "coding_agents",
    "config",
    "context",
    "flows",
    "gateways",
    "inference",
    "jobs",
    "ledger",
    "mcp",
    "memory",
    "orchestrator",
    "skills",
    "tools",
    "utils",
    "web",
)


def test_every_setting_has_a_reader() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for folder in _SOURCE_DIRS
        for path in (_ROOT / folder).rglob("*.py")
        if path != _ROOT / "config" / "settings.py"
    )
    settings_file = (_ROOT / "config" / "settings.py").read_text(encoding="utf-8")

    unread = [
        name
        for name in Settings.model_fields
        # By name in any form: provider keys are read with getattr, from a table of names.
        if not re.search(rf"\b{name}\b", source) and not re.search(rf"self\.{name}\b", settings_file)
    ]

    assert not unread
