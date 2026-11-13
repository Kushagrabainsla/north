"""Pack the saved patches into a kit that scores them on any x86 machine with Docker, and read the scores back.

    python experiments/harness_benchmark/kit.py build     # writes scoring-kit.tar.gz in the results folder
    python experiments/harness_benchmark/kit.py check KIT # opens a kit and checks every task and patch is there
    python experiments/harness_benchmark/kit.py report    # after the scores come back: pass rates per setup

This Mac has neither x86 nor the disk for the images (over 40 GB), so scoring runs elsewhere. The kit holds
the scorer, the task files with paths relative to the benchmark folder, the task folders they name, and one
results folder with the run to score for each setup and task. `score_on_droplet.sh` in it does the rest.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from catalog import CACHE, RESULTS_DIR, TASKS_DIR  # noqa: E402

_HERE = Path(__file__).parent
_HARNESS_FILES = ("score.py", "catalog.py", "replay_agent.py")
_FINAL = "final"
_KIT_NAME = "scoring-kit"
HARBOR_VERSION = "0.25.0"  # the version the replay agent was checked against
# The run to score for each task: north's latest, after every fix the benchmark loop made.
NORTH_RUNS = {
    1: "loop-b1", 2: "loop-b1", 3: "loop-b1", 4: "loop-b2", 5: "loop-b2-r2", 6: "loop-b2",
    7: "loop-rev1", 8: "loop-rev2", 9: "loop-rev1", 10: "loop-r3", 11: "loop-r3", 12: "loop-rev6",
    13: "loop-rev6", 14: "loop-b6", 16: "loop-b7", 17: "loop-b6", 18: "loop-b7", 19: "loop-b7", 20: "loop-b8",
}  # fmt: skip
PLAIN_RUNS = "plain-v2"

_DROPLET_SCRIPT = """#!/usr/bin/env bash
# Score every patch in this kit. Run on an x86 Linux machine with Docker, as a user who may run docker.
set -euo pipefail
KIT="$(cd "$(dirname "$0")" && pwd)"
BENCH="$HOME/.cache/north/harness-benchmark"
mkdir -p "$BENCH"
cp -R "$KIT/cache/." "$BENCH/"
if ! command -v uv >/dev/null; then curl -LsSf https://astral.sh/uv/install.sh | sh; fi
export PATH="$HOME/.local/bin:$PATH"
docker info >/dev/null
cd "$KIT/harness"
HARBOR=(uvx --from "harbor=={harbor}" --with httpx python)
echo "== proving the scorer: each reference fix must pass, an empty change must fail"
"${{HARBOR[@]}}" score.py --check 2
"${{HARBOR[@]}}" score.py --check 16
echo "== scoring every patch (hours; progress in $KIT/score.log)"
"${{HARBOR[@]}}" score.py --name {final} 2>&1 | tee "$KIT/score.log"
tar czf "$KIT/scores.tar.gz" -C "$BENCH/results/{final}" --exclude=jobs .
echo "== done: copy $KIT/scores.tar.gz back to the Mac"
"""


def _relative(task: dict) -> dict:
    """The task with its score paths relative to the benchmark folder, so it opens under any home."""
    score = dict(task.get("score") or {})
    for key in ("folder", "gold"):
        if key in score:
            score[key] = str(Path(score[key]).relative_to(CACHE))
    return {**task, "score": score}


def _did_work(record: dict) -> bool:
    """A run that never started (Codex out of credits, a refused sign-in) is not scored as a failure."""
    return not (record.get("error") and not record.get("patch_bytes"))


def _runs_to_score() -> list[Path]:
    """The record of each run to score: north's latest per task, and each plain run that did work."""
    chosen = [RESULTS_DIR / folder / f"north__{task:02d}__s1.json" for task, folder in NORTH_RUNS.items()]
    chosen += sorted((RESULTS_DIR / PLAIN_RUNS).glob("*__*__s1.json"))
    return [path for path in chosen if _did_work(json.loads(path.read_text()))]


def build(dest: Path) -> Path:
    """Write the kit to *dest* and return the archive's path."""
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch) / _KIT_NAME
        (root / "harness").mkdir(parents=True)
        for name in _HARNESS_FILES:
            shutil.copy2(_HERE / name, root / "harness" / name)
        tasks = root / "cache" / "tasks"
        tasks.mkdir(parents=True)
        for file in sorted(TASKS_DIR.glob("*.json")):
            task = _relative(json.loads(file.read_text()))
            (tasks / file.name).write_text(json.dumps(task, indent=1))
            if "folder" in task["score"]:
                shutil.copytree(CACHE / task["score"]["folder"], root / "cache" / task["score"]["folder"])
        final = root / "cache" / "results" / _FINAL
        final.mkdir(parents=True)
        for record in _runs_to_score():
            shutil.copy2(record, final / record.name)
            shutil.copy2(record.with_suffix(".patch"), final / record.with_suffix(".patch").name)
        script = root / "score_on_droplet.sh"
        script.write_text(_DROPLET_SCRIPT.format(harbor=HARBOR_VERSION, final=_FINAL))
        script.chmod(0o755)
        archive = dest / f"{_KIT_NAME}.tar.gz"
        with tarfile.open(archive, "w:gz") as packed:
            packed.add(root, arcname=_KIT_NAME)
    return archive


def check(archive: Path) -> list[str]:
    """What is missing from *archive*: a task folder, a gold patch, or a patch for a record."""
    problems = []
    with tempfile.TemporaryDirectory() as scratch:
        with tarfile.open(archive) as packed:
            packed.extractall(scratch, filter="data")
        cache = Path(scratch) / _KIT_NAME / "cache"
        for file in sorted((cache / "tasks").glob("*.json")):
            score = json.loads(file.read_text()).get("score") or {}
            for key in ("folder", "gold"):
                if key in score and (Path(score[key]).is_absolute() or not (cache / score[key]).exists()):
                    problems.append(f"task {file.stem}: {key} {score[key]} is not in the kit")
        for record in sorted((cache / "results" / _FINAL).glob("*.json")):
            if not record.with_suffix(".patch").exists():
                problems.append(f"{record.name}: no patch")
    return problems


def report(folder: Path) -> str:
    """Pass rate per setup over the tasks every setup ran, from the scored records in *folder*."""
    scored: dict[str, dict[int, bool]] = {}
    for file in folder.glob("*__*__s1.json"):
        record = json.loads(file.read_text())
        if "score" in record:
            scored.setdefault(record["arm"], {})[record["number"]] = bool(record["score"]["passed"])
    shared = set.intersection(*(set(tasks) for tasks in scored.values())) if scored else set()
    lines = [f"tasks every setup ran: {sorted(shared)}"]
    for arm, tasks in sorted(scored.items()):
        passed = sum(tasks[n] for n in shared)
        lines.append(f"{arm}: {passed}/{len(shared)} passed on the shared tasks; all its tasks: "
                     f"{sum(tasks.values())}/{len(tasks)}")  # fmt: skip
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("build", "check", "report"))
    parser.add_argument("path", nargs="?")
    args = parser.parse_args()
    if args.command == "build":
        archive = build(RESULTS_DIR)
        problems = check(archive)
        print(f"wrote {archive}" if not problems else "\n".join(problems))
        return 1 if problems else 0
    if args.command == "check":
        problems = check(Path(args.path))
        print("\n".join(problems) or "the kit is complete")
        return 1 if problems else 0
    print(report(Path(args.path) if args.path else RESULTS_DIR / _FINAL))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
