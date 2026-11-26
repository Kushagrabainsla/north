"""What a Codex run may touch, as the filesystem part of its permission profile.

Codex's own `:workspace` profile lets an agent read the whole disk. A deny-list of credential folders
(`~/.ssh`, `~/.aws`, ...) is never complete: a secret in `~/.config/<anything>`, `~/.kube` or `~/.netrc` was
readable. So the home folder is closed by default and opened only for what a run needs. Entries are listed
explicitly because a glob deny overrides a later open, and a deny on the home folder itself breaks git.

Claude Code works the same way by default (reads outside the working folder are blocked), so both agents now
see a closed home and the folder they were given.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path

from coding_agents.models import Mode

# Read-only: toolchains an agent runs commands with. None of them holds credentials.
HOME_TOOLCHAINS: tuple[str, ...] = (
    ".local/bin",
    ".local/share/uv",
    ".pyenv",
    ".nvm",
    ".volta",
    ".asdf",
    ".rbenv",
    ".rustup",
    ".cargo/bin",
    ".bun",
    ".deno",
    ".sdkman",
    "go",
    ".gitconfig",
)
# Git's own files under `~/.config`: its config, the global ignore and attributes. Read-only, as `.gitconfig`
# is. Only these files, because git can keep a credential store in the same folder. They are listed even
# when they do not exist: git looks for them on every command, and a closed `~/.config` made each git
# command warn "unable to access '~/.config/git/ignore'" (279 lines in 116 runs on north's coding path), and the user's
# global ignore was not used.
HOME_GIT_FILES: tuple[str, ...] = (".config/git/config", ".config/git/ignore", ".config/git/attributes")
# Package-manager caches: written by installs, read by builds.
HOME_CACHES: tuple[str, ...] = (".cache", ".npm", "Library/Caches", ".gradle", ".m2")
# Where north keeps the isolated copies. One run must not read or write another run's copy.
COPIES_DIR = ".cache/north"


def codex_filesystem(
    workspace: str,
    mode: Mode,
    protected: Iterable[str],
    command_dirs: Iterable[str] = (),
    home: Path | None = None,
    scratch: str | None = None,
) -> dict[str, str]:
    """Path -> "read" | "write" | "deny" for one run. *workspace* is the folder the agent was given.

    *scratch* is the run's own temp folder, writable in both modes. North deletes it after the run, so
    a plan run still changes nothing. Without it a plan run could not run a heredoc: the shell writes
    the text to a temp file first.
    """
    home = home or Path.home()
    editing = mode is Mode.EDIT
    rules: dict[str, str] = {}
    for relative in HOME_TOOLCHAINS:
        if (home / relative).exists():
            rules[str(home / relative)] = "read"
    for relative in HOME_GIT_FILES:
        rules[str(home / relative)] = "read"
    for relative in HOME_CACHES:
        if (home / relative).exists():
            rules[str(home / relative)] = "write" if editing else "read"
    rules[str(home / ".codex")] = "write"  # the agent's own login and session files
    for directory in command_dirs:
        rules[directory] = "read"
    if scratch:
        rules[os.path.realpath(scratch)] = "write"
    rules.update(_close_copies(home / COPIES_DIR, os.path.realpath(workspace)))
    rules.update(_workspace_rules(workspace, editing))
    rules.update(_close(home, [path for path, access in rules.items() if access != "deny"]))
    for path in protected:  # last, so nothing above can reopen a secret
        rules[os.path.expanduser(path)] = "deny"
    return rules


def closed_paths(workspace: str, mode: Mode, protected: Iterable[str], home: Path | None = None) -> list[str]:
    """What a run may not read: the paths `codex_filesystem` denies, for an agent whose sandbox takes a list.

    Claude Code's sandbox took only north's short secret list, so a plan run read a file in `~/.config/<any>`,
    and its Read tool read inside `~/.north` (measured 2026-11-25). Both agents now see the same closed home.
    """
    rules = codex_filesystem(workspace, mode, protected, home=home)
    return sorted(path for path, access in rules.items() if access == "deny")


def _close(home: Path, open_paths: Iterable[str]) -> dict[str, str]:
    """Deny every entry of the home folder that is not open, and is not on the way to something that is.

    The home folder itself is not denied: git and others look at each folder on the way to a path before
    they use it, and a denied home made even `git status` in a copy fail. Entries are listed when the run
    starts, so a file made in the home folder during the run is the one thing this does not cover.
    """
    keep = {str(home)}
    for path in open_paths:
        keep.update(str(parent) for parent in Path(path).parents if home in Path(path).parents)
    opened = set(open_paths)
    denied: dict[str, str] = {}
    pending = [home]
    while pending:
        folder = pending.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            if entry.path in opened:
                continue
            if entry.path in keep:
                pending.append(Path(entry.path))
            else:
                denied[entry.path] = "deny"
    return denied


def _close_copies(copies: Path, workspace: str) -> dict[str, str]:
    """Close the other copies in *copies*, and keep the folders on the way to this run's copy readable.

    A deny on the whole copies folder also hid the folders above the run's own copy. Node, npm and a
    virtualenv's python look at each folder on the way to their own path, so `npm test`, `npm run build`
    and `.venv/bin/python` failed with "operation not permitted" in a copy (benchmark, 2026-11-12). Each
    other copy is denied instead. The folders on the way are `read`, not left to the writable cache
    around them, so a copy that another run makes during this run is never writable from this one.
    """
    root = os.path.realpath(copies)
    if not workspace.startswith(root + os.sep):
        return {root: "deny"}
    rules: dict[str, str] = {}
    folder = Path(root)
    target = Path(workspace)
    while folder != target:
        rules[str(folder)] = "read"
        step = folder / target.relative_to(folder).parts[0]
        try:
            entries = list(os.scandir(folder))
        except OSError:
            entries = []
        for entry in entries:
            if entry.path != str(step):
                rules[entry.path] = "deny"
        folder = step
    return rules


def _workspace_rules(workspace: str, editing: bool) -> dict[str, str]:
    root = os.path.realpath(workspace)
    rules = {root: "write" if editing else "read"}
    git = Path(root) / ".git"
    if git.is_file():  # a linked copy: `.git` is a pointer to north's repository
        rules[str(git)] = "read"  # deleting or repointing it would break the copy, or aim commits at the real repo
        admin = _gitdir(git)
        if admin is not None:
            rules[os.path.dirname(os.path.dirname(admin))] = "read"  # history and objects
            rules[admin] = "write" if editing else "read"  # this copy's own index
    return rules


def _gitdir(pointer: Path) -> str | None:
    """The directory a linked worktree's `.git` file points at, or None when it does not say."""
    try:
        text = pointer.read_text().strip()
    except OSError:
        return None
    return os.path.realpath(text[len("gitdir:") :].strip()) if text.startswith("gitdir:") else None


def profile(base: str, name: str, filesystem: Mapping[str, str]) -> dict[str, object]:
    """The `config` Codex's thread/start takes: *base* with *filesystem* applied."""
    return {"default_permissions": name, "permissions": {name: {"extends": base, "filesystem": dict(filesystem)}}}
