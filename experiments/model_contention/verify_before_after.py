"""Bucket 3, before and after: the background loops of #8 in the code of a checkout.

Run it on main (before) and on the PR branch (after). It reads the source, not the
running server, and reports:

- whether EpisodeConsolidator and SkillDistiller have an endless `run()` loop;
- every call of `.run()` and `.run_once()` on a consolidator or distiller in north's
  own code (tests and experiments left out), found by parsing each file.

A loop that exists and that no code calls is dead: deleting it cannot change what
north does. The paths that do run these parts (task end, hourly flows) must be the
same in both versions.

Writes results/verify_<label>.json next to this file:
    .venv/bin/python experiments/model_contention/verify_before_after.py <label> [checkout]
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

REPO = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "results"
SKIP = ("tests", "experiments", ".venv", "node_modules", "web", "guides", "site")


def loops(path: Path, class_name: str) -> dict[str, bool]:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            methods = {n.name: n for n in node.body if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)}
            run = methods.get("run")
            endless = run is not None and any(
                isinstance(n, ast.While) and isinstance(n.test, ast.Constant) and n.test.value is True
                for n in ast.walk(run)
            )
            return {"has_run_method": run is not None, "run_is_endless_loop": endless}
    return {"has_run_method": False, "run_is_endless_loop": False}


def calls() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {"run": [], "run_once": []}
    for path in sorted(REPO.rglob("*.py")):
        rel = path.relative_to(REPO)
        if rel.parts[0] in SKIP:
            continue
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in found:
                continue
            owner = ast.unparse(node.func.value).lower()
            if "consolidator" in owner or "distiller" in owner:
                found[node.func.attr].append(f"{rel}:{node.lineno} {ast.unparse(node.func)}()")
    return found


def main() -> None:
    label = sys.argv[1] if len(sys.argv) > 1 else "run"
    report = {
        "label": label,
        "EpisodeConsolidator": loops(REPO / "memory" / "consolidator.py", "EpisodeConsolidator"),
        "SkillDistiller": loops(REPO / "skills" / "distiller.py", "SkillDistiller"),
        "calls_in_north_code": calls(),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"verify_{label}.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
