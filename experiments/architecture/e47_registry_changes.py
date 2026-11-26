"""#47: how often did one commit have to change more than one of the five registries?

Usage: e47_registry_changes.py

Reads `git log` of this checkout since 2026-09-01 and counts commits that touched each registry module,
and commits that touched two or more. Read-only; the results hold counts only.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS = Path(__file__).resolve().parent / "results"
REGISTRIES = {
    "agents": "agents/registry.py",
    "tools": "tools/registry.py",
    "skills": "skills/registry.py",
    "flows": "flows/registry.py",
    "mcp": "mcp/manager.py",
}


def main() -> None:
    missing = [path for path in REGISTRIES.values() if not (ROOT / path).exists()]
    assert not missing, f"registry modules moved: {missing}"
    log = subprocess.run(
        ["git", "-C", str(ROOT), "log", "--since=2026-09-01", "--format=@%h", "--name-only", "origin/main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    per_registry: Counter[str] = Counter()
    together: Counter[int] = Counter()
    commits = 0
    for block in log.split("@")[1:]:
        commits += 1
        files = set(block.split("\n")[1:])
        touched = [name for name, path in REGISTRIES.items() if path in files]
        per_registry.update(touched)
        if touched:
            together[len(touched)] += 1
    result = {
        "commits_since_2026_09_01": commits,
        "commits_touching_each_registry": dict(per_registry),
        "commits_by_number_of_registries_touched": dict(sorted(together.items())),
    }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "e47_registry_changes.json").write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
