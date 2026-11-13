"""One task's working copy on this Mac, identical for every setup, and the patch a run leaves in it."""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from catalog import SOURCES_DIR, Task

from utils.filesystem import PRUNED_DIRS

_GIT = ("git", "-c", "user.name=bench", "-c", "user.email=bench@bench", "-c", "commit.gpgsign=false")
# Installed dependencies and caches: on disk for the agents, never part of a patch, and not added to the project.
_EXCLUDED = (".venv/", "node_modules/", "__pycache__/", ".pytest_cache/", ".next/", ".mypy_cache/", "*.pyc")
_SETUP_TIMEOUT_S = 1200


@dataclass(frozen=True)
class Checkout:
    """Fields named as `coding_paths_comparison`'s arms expect them, so its raw arms run unchanged."""

    path: Path
    seed: str
    prompt: str
    setup_failed: list[str] = field(default_factory=list)


def localize(text: str, root: str | None, path: Path) -> str:
    """Point a benchmark's container path (/app, /workspace) at the copy on this machine."""
    if not root:
        return text
    return re.sub(rf"(?<![\w.]){re.escape(root)}(?![\w-])", str(path), text)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run([*_GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _mirror(repo: str) -> Path:
    """A local object store for *repo* that fetches file contents only when they are needed."""
    mirror = SOURCES_DIR / "repos" / f"{repo.replace('/', '__')}.git"
    if not mirror.exists():
        mirror.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://github.com/{repo}"
        subprocess.run(["git", "clone", "-q", "--bare", "--filter=blob:none", url, str(mirror)], check=True)
    return mirror


def _have(mirror: Path, commit: str) -> None:
    done = subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=mirror, capture_output=True)
    if done.returncode != 0:
        subprocess.run(["git", "fetch", "-q", "origin", commit], cwd=mirror, check=True)


def _export(spec: dict[str, Any], dest: Path) -> None:
    """The tree at a commit, with no history: a run must not be able to read the fix out of the log.

    Submodules too, each at the commit the project pins: `git archive` leaves them out, and
    openlibrary's copy had no `vendor/infogami`, so no setup could import the code it was to change.
    """
    mirror = _mirror(spec["repo"])
    _have(mirror, spec["commit"])
    archive = subprocess.run(["git", "archive", spec["commit"]], cwd=mirror, check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")
    for path, repo in _submodules(mirror, spec["commit"]):
        pinned = _git(mirror, "ls-tree", spec["commit"], path).split()
        if len(pinned) >= 3 and pinned[1] == "commit":
            (dest / path).mkdir(parents=True, exist_ok=True)
            _export({"repo": repo, "commit": pinned[2]}, dest / path)


_GITHUB = re.compile(r"github\.com[/:]([^/]+/[^/]+?)(?:\.git)?/?$")


def _submodules(mirror: Path, commit: str) -> list[tuple[str, str]]:
    """(path, GitHub owner/name) of each submodule the commit's `.gitmodules` lists."""
    listed = subprocess.run(["git", "show", f"{commit}:.gitmodules"], cwd=mirror, capture_output=True, text=True)
    sections: list[dict[str, str]] = []
    for line in listed.stdout.splitlines():
        if line.strip().startswith("["):
            sections.append({})
        elif sections and "=" in line:
            key, _, value = line.partition("=")
            sections[-1][key.strip()] = value.strip()
    return [
        (section["path"], repo.group(1))
        for section in sections
        if "path" in section and (repo := _GITHUB.search(section.get("url", "")))
    ]


def _clone_with_history(spec: dict[str, Any], dest: Path) -> None:
    """A review reads the history, so it gets one; it changes nothing."""
    mirror = _mirror(spec["repo"])
    for commit in [spec["commit"], *spec.get("also", [])]:
        _have(mirror, commit)
    url = f"https://github.com/{spec['repo']}"
    subprocess.run(["git", "clone", "-q", "--filter=blob:none", "--no-checkout", url, str(dest)], check=True)
    for commit in [spec["commit"], *spec.get("also", [])]:
        subprocess.run(["git", "fetch", "-q", "origin", commit], cwd=dest, check=True)
    subprocess.run(["git", "checkout", "-q", "--detach", spec["commit"]], cwd=dest, check=True)


def _fill(task: Task, dest: Path) -> None:
    spec = task.checkout
    if spec["kind"] == "git":
        _export(spec, dest)
    elif spec["kind"] == "dir":
        shutil.copytree(spec["path"], dest / spec["into"], dirs_exist_ok=True)
    else:
        for name, text in spec["files"].items():
            file = dest / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(localize(text, spec.get("root"), dest))


def _apply(dest: Path, patches: list[str]) -> None:
    for patch in patches:
        subprocess.run(
            [*_GIT, "apply", "--whitespace=nowarn", "-"],
            cwd=dest,
            input=patch,
            text=True,
            check=True,
            capture_output=True,
        )


def _set_up(task: Task, dest: Path) -> list[str]:
    failed = []
    for command in task.setup:
        try:
            done = subprocess.run(
                command, cwd=dest, shell=True, capture_output=True, text=True, timeout=_SETUP_TIMEOUT_S, env=os.environ
            )
            if done.returncode != 0:
                failed.append(f"{command}: {(done.stdout + done.stderr)[-300:]}")
        except subprocess.TimeoutExpired:
            failed.append(f"{command}: timed out")
    return failed


def materialize(task: Task, dest: Path) -> Checkout:
    """A fresh committed copy of the task, with the same dependencies for every setup."""
    spec = task.checkout
    if spec.get("keep_history"):
        _clone_with_history(spec, dest)
        return Checkout(dest, _git(dest, "rev-parse", "HEAD").strip(), task.prompt)
    dest.mkdir(parents=True)
    _fill(task, dest)
    _git(dest, "init", "-q")
    _apply(dest, spec.get("patches", []))
    # Every file the project tracks, even one its own .gitignore names: flipt's `flipt` line matched
    # `cmd/flipt`, the seed went without it, and the agent wrote the server from scratch.
    _git(dest, "add", "-A", "--force")
    (dest / ".git" / "info" / "exclude").write_text("\n".join(_EXCLUDED) + "\n")
    failed = _set_up(task, dest)  # before the seed: what an install leaves (`*.egg-info`) is not the agent's change
    _git(dest, "add", "-A")
    _git(dest, "commit", "-qm", "seed", "--allow-empty")
    seed = _git(dest, "rev-parse", "HEAD").strip()
    return Checkout(dest, seed, localize(task.prompt, spec.get("root"), dest), failed)


def capture_patch(path: Path, seed: str) -> str:
    """Everything the run changed since the seed, committed or not, without the build output it left.

    Plain Claude built task 9 in place, and its patch carried `build/regression_test` and its debug files:
    macOS programs that `make` in the Linux scoring image could take for up to date. A new file is left
    out when it is binary or sits in a build or dependency folder; a file the seed had always stays.
    """
    _git(path, "add", "-A")
    generated = _build_output(path, seed)
    if generated:
        _git(path, "reset", "-q", "--", *(f":(literal){name}" for name in generated))
    return _git(path, "diff", "--cached", "--binary", seed)


def _descends_from(path: Path, seed: str, branch: str) -> bool:
    found = subprocess.run([*_GIT, "merge-base", "--is-ancestor", seed, branch], cwd=path, capture_output=True)
    return found.returncode == 0


def _build_output(path: Path, seed: str) -> list[str]:
    """The staged files new since *seed* that a build made, not an author."""
    added = set(_git(path, "diff", "--cached", "--name-only", "--diff-filter=A", "-z", seed).split("\0")) - {""}
    binary = {
        line.split("\t", 2)[2]
        for line in _git(path, "diff", "--cached", "--numstat", seed).splitlines()
        if line.startswith("-\t-\t")
    }
    return sorted(
        name for name in added if name in binary or any(part in PRUNED_DIRS for part in Path(name).parts[:-1])
    )


def kept_branches(path: Path, seed: str) -> dict[str, str]:
    """Patches on branches a run left behind instead of applying: name -> diff from the seed."""
    names = _git(path, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    current = _git(path, "branch", "--show-current").strip()
    kept = {}
    for name in names:
        # A run's branch starts at the seed. A review task's checkout has the project's own branches too,
        # and task 18's "kept" patch was a 160 MB diff to one of them, for a run that changed nothing.
        if name != current and _descends_from(path, seed, name):
            diff = _git(path, "diff", "--binary", seed, name)
            if diff:
                kept[name] = diff
    return kept
