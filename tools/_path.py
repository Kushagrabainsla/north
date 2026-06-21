"""Shared path resolution and sensitive-path policy for workspace-scoped tools.

This module is the single fail-closed gate for filesystem access: every tool
that touches a path (read_file, write_file, search_files, glob, list_dir,
check_types, search_symbols, find_references) resolves it through
``resolve_path()``, and every tool that walks a tree prunes via
``PRUNED_DIRS``/``SENSITIVE_DIR_NAMES``. The sensitive-path blocklist applies
in *all* branches - with or without a workspace - so setting a broad workspace
(e.g. $HOME) can never re-open access to ~/.ssh, ~/.north, /etc, etc.

The sole exception is ``<NORTH_HOME>/tasks/`` (see ``_handoff_root``): a narrow,
always-allowed carve-out for internal agent handoff files. Secrets and all DB
files inside ~/.north stay blocked even there.

See docs/CODING_STYLE.md Section 16.1.1.
"""

from __future__ import annotations

import functools
import os
import sys
from pathlib import Path

# Directories never worth walking for a coding task. Shared by the
# file-walking tools (search_files, glob) so the exclusion list cannot drift.
PRUNED_DIRS: frozenset[str] = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv", ".ruff_cache", ".pytest_cache", "build", "dist"}
)

# Home-relative directory names that hold credentials/secrets. File-walking
# tools must skip these even when the walk root (e.g. a $HOME workspace)
# legitimately contains them.
SENSITIVE_DIR_NAMES: frozenset[str] = frozenset({".ssh", ".aws", ".gnupg", ".config", ".north"})

# Well-known sensitive directories blocked regardless of workspace.
_BLOCKED_PREFIXES: tuple[str, ...] = (
    "/etc",
    "/proc",
    "/sys",
    "/dev",
    "/boot",
    "/bin",
    "/sbin",
    "/usr/bin",
    "/usr/sbin",
    "/var/run",
    "/private/etc",  # macOS
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.config",
    "~/.north",  # north's own home: secret.key, .env, ledger DBs
)

# Filenames whose contents are secrets wherever they live.
_BLOCKED_FILENAMES: frozenset[str] = frozenset({"secret.key"})

# SQLite state suffixes. north's own DBs (ledger, jobs, task-context, ...) live
# under ~/.north - including the task-context DBs that share ~/.north/tasks/ with
# the agent handoff carve-out. Agents must never write these directly; they go
# through the proper stores/tools. Enforced only inside ~/.north so a workspace
# may still contain editable .db files.
_DB_SUFFIXES: tuple[str, ...] = (".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3")

# Marker files that identify a project root, checked from a file upward.
_PROJECT_ROOT_MARKERS: tuple[str, ...] = (
    ".git",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "go.mod",
    "package.json",
    "Cargo.toml",
)


@functools.lru_cache(maxsize=1)
def _resolved_blocked_prefixes() -> tuple[str, ...]:
    """Expand and resolve _BLOCKED_PREFIXES once; falls back to the raw prefix."""
    resolved: list[str] = []
    for prefix in _BLOCKED_PREFIXES:
        try:
            resolved.append(str(Path(prefix).expanduser().resolve()))
        except Exception:
            resolved.append(prefix)
    return tuple(resolved)


@functools.lru_cache(maxsize=1)
def _handoff_root() -> str:
    """Absolute path of the per-task handoff area: ``<NORTH_HOME>/tasks``.

    Derived from the same ``NORTH_HOME`` override as ``config.settings`` (kept in
    sync by env, not import, so this module stays dependency-light). This single
    subtree is the *only* writable carve-out inside the otherwise-blocked
    ``~/.north`` home - secrets (``secret.key``), DBs, and ``.env`` live in the
    home root, outside ``tasks/``, so they remain blocked.
    """
    base = Path(os.environ.get("NORTH_HOME", "~/.north")).expanduser()
    try:
        return str((base / "tasks").resolve())
    except Exception:
        return str(base / "tasks")


def _in_handoff_root(resolved: str) -> bool:
    """True when *resolved* is the handoff root or a path strictly under it."""
    root = _handoff_root()
    return resolved == root or resolved.startswith(root + "/")


def handoff_dir_for(task_id: str) -> str:
    """Absolute per-task handoff directory: ``<NORTH_HOME>/tasks/<task_id>``.

    The single source of truth for where agents write internal pipeline
    artifacts (research notes, specs, QA reports). Injected into agent prompts
    so paths are never hardcoded or workspace-relative.
    """
    return f"{_handoff_root()}/{task_id}"


def _is_blocked_path(resolved: str) -> bool:
    """True when *resolved* (an absolute path string) sits under a blocked prefix."""
    if Path(resolved).name in _BLOCKED_FILENAMES:
        return True
    # Carve-out: the per-task handoff area is writable agent scratch even though
    # the rest of ~/.north is blocked. Checked after the filename block above so
    # a secret.key inside tasks/ can never be exposed. The task-context SQLite
    # DBs share ~/.north/tasks/ with this carve-out, so DB files stay blocked.
    if _in_handoff_root(resolved):
        return resolved.endswith(_DB_SUFFIXES)
    return any(resolved == prefix or resolved.startswith(prefix + "/") for prefix in _resolved_blocked_prefixes())


def is_sensitive_path(path: Path) -> bool:
    """True when *path* resolves into a blocked sensitive directory or filename.

    Used by file-walking tools to prune sensitive subtrees that live under an
    otherwise-allowed walk root. Matches both the absolute blocklist and the
    name-based credential dirs (.ssh, .aws, ...) so the policy is identical
    whether a tree is walked or a file inside it is targeted directly.
    """
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, ValueError):
        return True  # unresolvable - treat as sensitive (fail closed)
    if any(part in SENSITIVE_DIR_NAMES for part in resolved.parts):
        return True
    return _is_blocked_path(str(resolved))


def references_sensitive_path(command: str) -> bool:
    """True when any path-like token in *command* points into a blocked directory.

    Shared by tools that pass raw command strings to a shell (e.g. BashTool's
    instant-safe fast path) and therefore never go through resolve_path().

    A parent-directory escape (any ``..`` segment) can read arbitrarily far above
    the working directory, and the fast path cannot prove where it lands, so any
    such token is treated as sensitive. Absolute and ``~`` tokens are checked
    against the blocklist directly; other relative paths are resolved against the
    current working directory before the same blocklist applies.
    """
    for token in command.split():
        candidate = token.strip("'\"")
        if not candidate or candidate.startswith("-"):
            continue  # empty fragment or a flag, not a path
        if ".." in Path(candidate).parts:
            return True  # parent-directory escape - never fast-path it
        if candidate.startswith(("/", "~")):
            base = Path(candidate).expanduser()
        elif "/" in candidate:
            base = Path.cwd() / candidate
        else:
            continue  # bare word (e.g. a grep pattern), not a path
        try:
            resolved = str(base.resolve())
        except (OSError, ValueError):
            return True  # unresolvable path-like token - treat as sensitive
        if _is_blocked_path(resolved):
            return True
    return False


def resolve_path(path_str: str, workspace: str | None) -> Path | None:
    """Resolve *path_str*, optionally scoped to *workspace*.

    - With workspace: resolved path must stay inside the workspace root.
    - Without workspace: path is resolved from CWD.
    - In *both* cases the sensitive-path blocklist applies, so a workspace of
      $HOME (or any broad root) never grants access to ~/.ssh, ~/.north, /etc…

    Returns ``None`` when the path is denied.
    """
    p = Path(path_str).expanduser()

    if p.is_absolute():
        candidate = p.resolve()
    elif workspace:
        candidate = (Path(workspace).resolve() / p).resolve()
    else:
        candidate = (Path.cwd() / p).resolve()

    # The per-task handoff area is an always-allowed write zone (internal agent
    # scratch) regardless of the active workspace, so it must not be rejected by
    # the workspace-containment check. The blocklist below still applies.
    if workspace and not _in_handoff_root(str(candidate)):
        root = Path(workspace).resolve()
        if not str(candidate).startswith(str(root) + "/") and candidate != root:
            return None

    if sys.platform != "win32" and _is_blocked_path(str(candidate)):
        return None

    return candidate


def find_project_root(path: Path, markers: tuple[str, ...] = _PROJECT_ROOT_MARKERS) -> Path:
    """Return the nearest enclosing directory containing one of *markers*.

    Walks upward from *path* (its parent when *path* is a file). Falls back to
    the file's own directory when no marker is found. Shared by check_types and
    the semantic tools so toolchain/config discovery is consistent.
    """
    start = path if path.is_dir() else path.parent
    for directory in (start, *start.parents):
        if any((directory / marker).exists() for marker in markers):
            return directory
    return start
