"""What north has for Kasa right now: secrets by name, tools and panels it made, environments. Read-only.

Prints names, counts and booleans only: never a secret's value.
"""

from __future__ import annotations

import json
from pathlib import Path

HOME = Path.home() / ".north"


def state() -> dict:
    store = HOME / "secrets.json"
    secrets = json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}
    # A glob over a folder that does not exist finds nothing.
    tools = sorted(str(p.relative_to(HOME)) for p in (HOME / "learned" / "tools").rglob("*.py"))
    sidecars = sorted(str(p.relative_to(HOME)) for p in (HOME / "learned" / "tools").rglob("*.json"))
    views = sorted(p.name for p in (HOME / "views").glob("*.json"))
    drafts = sorted(p.name for p in (HOME / "views" / "drafts").glob("*.json"))
    envs = sorted(p.name for p in (HOME / "learned" / "envs").glob("*"))
    return {
        "kasa_secret_names": sorted(name for name in secrets if "kasa" in name),
        "learned_tool_files": tools,
        "learned_tool_sidecars": sidecars,
        "views": views,
        "view_drafts": drafts,
        "tool_environments": envs,
    }


if __name__ == "__main__":
    print(json.dumps(state(), indent=1))
