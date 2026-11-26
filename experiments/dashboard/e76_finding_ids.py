"""#76: which adversarial-test finding ids do north's commits and CHANGELOG name, and in which commits?

Usage: e76_finding_ids.py

The findings report itself is not in the repository. This lists every id of the form A1, M19, P9 or W14
that a commit message citing #76, or the CHANGELOG, names, with the commits that name it.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS = Path(__file__).resolve().parent / "results"
ID = re.compile(r"\b([AMPW]\d{1,2})\b")


def main() -> None:
    log = subprocess.run(
        ["git", "-C", str(ROOT), "log", "origin/main", "--format=@@%h %s%n%b"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    commits: dict[str, list[str]] = defaultdict(list)
    for block in log.split("@@")[1:]:
        sha, _, rest = block.partition(" ")
        if "#76" in rest:
            for found in sorted(set(ID.findall(rest))):
                commits[found].append(sha)
    changelog = (ROOT / "CHANGELOG.md").read_text()
    in_changelog = {found for line in changelog.splitlines() if "#76" in line for found in ID.findall(line)}
    ids = sorted(set(commits) | in_changelog, key=lambda x: (x[0], int(x[1:])))
    rows = [{"id": i, "commits": commits.get(i, []), "in_changelog": i in in_changelog} for i in ids]
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "e76_finding_ids.json").write_text(json.dumps(rows, indent=1) + "\n")
    print(f"{len(ids)} ids; with a commit: {sum(bool(r['commits']) for r in rows)}")
    for row in rows:
        print(row["id"], ",".join(row["commits"]) or "-", "changelog" if row["in_changelog"] else "")


if __name__ == "__main__":
    main()
