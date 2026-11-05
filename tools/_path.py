"""Shared path resolution and sensitive-path policy for workspace-scoped tools.

This module is the single fail-closed gate for filesystem access: every tool
that touches a path (read_file, write_file, search_files, glob, list_dir,
check_types, search_symbols, find_references) resolves it through
``resolve_path()``, and every tool that walks a tree prunes via
``PRUNED_DIRS``/``SENSITIVE_DIR_NAMES``. The sensitive-path blocklist applies
in *all* branches - with or without a workspace - so setting a broad workspace
(e.g. $HOME) can never re-open access to ~/.ssh, north's home, /etc, etc.

North's home is ``settings.north_home`` (NORTH_HOME), blocked wherever it is;
``~/.north`` stays blocked too, since a default install's secrets may sit there.
The exceptions are ``<NORTH_HOME>/tasks/`` (see ``_handoff_root``), a narrow,
always-allowed carve-out for internal agent handoff files, and the personal-data
subdirs. Secrets and all DB files inside north's home stay blocked even there.

See docs/CODING_STYLE.md Section 16.1.1.
"""

from __future__ import annotations

import functools
import shlex
import sys
from pathlib import Path

from config.settings import settings
from utils.edit_scope import EditAuthorizer

# The pure filesystem-traversal exclusion set now lives in the platform layer
# (``utils.filesystem``). It is re-exported here so existing ``tools._path``
# importers (search_files, glob, context.repo_map) keep working unchanged and so
# the file-walking tools share one source of truth for what to prune.
from utils.filesystem import (
    PRUNED_DIRS,  # noqa: F401  (re-exported for tools._path compatibility)
)

# The per-task handoff directory functions now live in the platform layer
# (``utils.handoff``), which owns where those directories are and their
# lifecycle. They are re-exported here so the sensitive-path gate in this module
# anchors its carve-out to the *same* cached ``_handoff_root`` and so existing
# ``tools._path`` importers (and their test seams) keep working unchanged.
from utils.handoff import (
    _handoff_root,
    ensure_handoff_dir,  # noqa: F401  (re-exported for tools._path compatibility)
    handoff_dir_for,  # noqa: F401  (re-exported for tools._path compatibility)
    prune_handoff_dirs,  # noqa: F401  (re-exported for tools._path compatibility)
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
    "~/.north",  # a default install's home: secret.key, .env, ledger DBs
)

# Filenames whose contents are secrets wherever they live.
_BLOCKED_FILENAMES: frozenset[str] = frozenset({"secret.key"})

# SQLite state suffixes. north's own DBs (ledger, jobs, task-context, ...) live
# in north's home - including the task-context DBs that share <home>/tasks/ with
# the agent handoff carve-out. Agents must never write these directly; they go
# through the proper stores/tools. Enforced only inside north's home so a workspace
# may still contain editable .db files. Public because the cockpit's artifact
# library reads that same directory and must exclude exactly these files.
DB_SUFFIXES: tuple[str, ...] = (".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3")

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
    """Expand and resolve _BLOCKED_PREFIXES and north's home once; falls back to the raw prefix.

    North's home is wherever NORTH_HOME put it. Blocking only ``~/.north`` left a
    moved home (Docker's /data) open: its .env and settings.json were readable.
    """
    resolved: list[str] = []
    for prefix in (*_BLOCKED_PREFIXES, str(settings.north_home)):
        try:
            resolved.append(str(Path(prefix).expanduser().resolve()))
        except Exception:
            resolved.append(prefix)
    return tuple(resolved)


# The per-task handoff directory functions now live in the platform layer
# (``utils.handoff``), which owns where those directories are and their
# lifecycle. They are re-exported here so the sensitive-path gate below can
# anchor its carve-out to the *same* cached ``_handoff_root`` and so existing
# ``tools._path`` importers (and their test seams) keep working unchanged.
def _in_handoff_root(resolved: str) -> bool:
    """True when *resolved* is the handoff root or a path strictly under it."""
    root = _handoff_root()
    return resolved == root or resolved.startswith(root + "/")


# Personal-data subdirectories inside NORTH_HOME that agents are allowed to
# write to (research digests, notes and plans), one folder per task inside. The rest of north's
# home stays blocked (secret.key, .env, ledger/jobs/facts DBs). Kept in sync with
# agent prompts so output never lands in the CWD/workspace.
_PERSONAL_SUBDIRS: tuple[str, ...] = ("news", "notes")


def _personal_root() -> str:
    """Absolute path of north's home (the personal-data parent)."""
    base = settings.north_home
    try:
        return str(base.resolve())
    except Exception:
        return str(base)


def _in_personal_subdir(resolved: str) -> bool:
    """True when *resolved* is one of the writable personal-data subdirs.

    Matches ``<NORTH_HOME>/news``, ``<NORTH_HOME>/notes``,
    ``<NORTH_HOME>/wellness`` or a path strictly under one of them - but never
    the NORTH_HOME root itself or any sibling (e.g. ``<NORTH_HOME>/tasks`` is
    handled separately by the handoff carve-out).
    """
    root = _personal_root()
    for sub in _PERSONAL_SUBDIRS:
        prefix = f"{root}/{sub}"
        if resolved == prefix or resolved.startswith(prefix + "/"):
            return True
    return False


def is_north_scratch(path: Path) -> bool:
    """True when *path* is in one of north's own writable areas.

    The per-task handoff directory and the personal-data subdirs (news, notes,
    wellness) are where agents are told to put their output. Writing there is
    north keeping its own notes, not touching the user's files. North's state
    DBs share the handoff directory and are never scratch.
    """
    resolved = str(path)
    if _in_handoff_root(resolved):
        return not resolved.endswith(DB_SUFFIXES)
    return _in_personal_subdir(resolved)


def personal_dir(subdir: str) -> str:
    """Absolute path of a writable personal-data subdir (``<NORTH_HOME>/<subdir>``).

    Used to inject concrete output dirs into agent prompts so they never write
    to the CWD/workspace. *subdir* must be one of the allowed names.
    """
    if subdir not in _PERSONAL_SUBDIRS:
        raise ValueError(f"unknown personal subdir: {subdir!r}")
    return f"{_personal_root()}/{subdir}"


def _is_blocked_path(resolved: str) -> bool:
    """True when *resolved* (an absolute path string) sits under a blocked prefix."""
    if Path(resolved).name in _BLOCKED_FILENAMES:
        return True
    # Carve-out: the per-task handoff area is writable agent scratch even though
    # the rest of north's home is blocked. Checked after the filename block above so
    # a secret.key inside tasks/ can never be exposed. The task-context SQLite
    # DBs share <home>/tasks/ with this carve-out, so DB files stay blocked.
    if _in_handoff_root(resolved):
        return resolved.endswith(DB_SUFFIXES)
    # Carve-out: the personal-data subdirs (news/notes/wellness) are the only
    # other writable part of north's home. The home root and every other sibling
    # (secret.key, .env, ledger/jobs/facts DBs) stay blocked.
    if _in_personal_subdir(resolved):
        return False
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
        # A path inside a sensitive-named dir is blocked by default - but the
        # personal-data subdirs in north's home (news/notes/wellness) are the
        # intended, readable output of agents, so carve them out. Everything
        # else in north's home (secret.key, .env, DBs) stays sensitive.
        return not _in_personal_subdir(str(resolved))
    return _is_blocked_path(str(resolved))


def target_path(path_str: str, workspace: str | None) -> Path:
    """Where *path_str* points: from *workspace* when relative, else from the CWD. No policy applied."""
    path = Path(path_str).expanduser()
    if not path.is_absolute():
        path = (Path(workspace) if workspace else Path.cwd()) / path
    try:
        return path.resolve()
    except (OSError, ValueError):
        return path


def _shell_tokens(command: str) -> list[str]:
    """*command*'s words, with operators (``&&``, ``|``, ``>``) as tokens of their own."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:  # unbalanced quotes: the plain words are still there
        return command.split()


_COMMAND_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "(", ")"})
_REDIRECTIONS = frozenset({">", ">>", ">|", "&>", "&>>"})


def _simple_commands(command: str) -> list[list[str]]:
    """*command* as its simple commands' words: `cd x && rm -rf y` is [["cd", "x"], ["rm", "-rf", "y"]]."""
    commands: list[list[str]] = [[]]
    for token in _shell_tokens(command):
        if token in _COMMAND_SEPARATORS:
            commands.append([])
        else:
            commands[-1].append(token)
    return [words for words in commands if words]


def _is_root(target: str) -> bool:
    """Whether *target*, as written, is /, a folder directly under it, or the home folder."""
    home = str(Path.home())
    for prefix in ("$HOME", "${HOME}", "~"):
        if target == prefix or target.startswith(prefix + "/"):
            target = home + target[len(prefix) :]
    path = Path(target.removesuffix("/*").rstrip("/") or "/")
    return path.is_absolute() and (path == Path(home) or path.parent == Path("/") or path == Path("/"))


def deletes_a_root(command: str) -> bool:
    """Whether *command* recursively deletes /, a folder directly under it, or the home folder.

    One of the few shapes refused outright instead of asked about. It was found by
    looking for the text "rm -rf /", so deleting one named folder by its absolute
    path was refused unseen, while `rm -rf ~` and `rm -fr /` were not recognised
    (issue #76, A12).
    """
    for words in _simple_commands(command):
        if words[0] == "sudo":
            words = words[1:]
        if not words or Path(words[0]).name != "rm":
            continue
        flags = [word for word in words[1:] if word.startswith("-")]
        recursive = any(
            flag == "--recursive" or (not flag.startswith("--") and set(flag[1:]) & {"r", "R"}) for flag in flags
        )
        if recursive and any(_is_root(word) for word in words[1:] if not word.startswith("-")):
            return True
    return False


def edited_paths(command: str, cwd: str | None) -> tuple[Path, ...]:
    """The files a shell command recognisably writes, resolved from *cwd*.

    Redirection and ``tee`` targets, and the files ``sed -i`` / ``perl -i`` edit
    in place. Recognisable, not exhaustive: a program that writes files itself
    is not seen, and still goes to a card like any other command.
    """
    found: list[Path] = []
    words: list[str] = []

    def add(token: str) -> None:
        if token and not token.startswith("&") and not token.startswith("/dev/"):
            path = target_path(token, cwd)
            if path not in found:
                found.append(path)

    def in_place_targets(args: list[str]) -> list[str]:
        targets, script_seen, skip = [], False, False
        for arg in args:
            if skip:
                skip, script_seen = False, True
            elif arg in ("-e", "--expression", "-f", "--file"):
                skip = True
            elif arg.startswith("-"):
                continue
            elif not script_seen:
                script_seen = True  # sed's script, when no -e gave one
            else:
                targets.append(arg)
        return targets

    def finish() -> None:
        if not words:
            return
        program, args = Path(words[0]).name, words[1:]
        if program == "tee":
            for arg in args:
                if not arg.startswith("-"):
                    add(arg)
        in_place = any(
            a == "--in-place" or (a.startswith("-") and not a.startswith("--") and "i" in a[1:]) for a in args
        )
        if program in ("sed", "perl") and in_place:
            for arg in in_place_targets(args):
                add(arg)
        words.clear()

    tokens = _shell_tokens(command)
    for index, token in enumerate(tokens):
        if token in _REDIRECTIONS:
            if index + 1 < len(tokens):
                add(tokens[index + 1])
        elif token in _COMMAND_SEPARATORS:
            finish()
        elif not (index and tokens[index - 1] in _REDIRECTIONS) and not set(token) <= set("<>&"):
            words.append(token)
    finish()
    return tuple(found)


def git_repository(path: Path) -> Path | None:
    """The git repository *path* is in, or None.

    Never the home folder or the root: a dotfiles repository there would put
    every file the user has inside a repository.
    """
    start = path if path.is_dir() else path.parent
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return None if directory in (Path.home(), Path("/")) else directory
    return None


def named_paths(command: str, cwd: str | None) -> tuple[Path, ...]:
    """The paths a shell command names, resolved from *cwd*: what a "no" to it covers.

    A token counts when it looks like a path (``/``, ``~``, or a ``/`` inside it)
    or names something that exists in *cwd*. Flags, operators, devices and bare
    words such as program names do not.
    """
    found: list[Path] = []
    for token in _shell_tokens(command):
        if not token or token.startswith("-") or set(token) <= set("&|;<>()"):
            continue
        looks_like_a_path = token.startswith(("/", "~")) or "/" in token
        if not (looks_like_a_path or (cwd and (Path(cwd) / token).exists())):
            continue
        path = target_path(token, cwd)
        if str(path).startswith("/dev/") or path in found:
            continue
        found.append(path)
    return tuple(found)


def resolve_path(path_str: str, workspace: str | None, *, contained: bool = True) -> Path | None:
    """Resolve *path_str*, optionally scoped to *workspace*.

    - With workspace: a relative path is resolved from it, and, when
      *contained*, the resolved path must stay inside it.
    - Without workspace: path is resolved from CWD.
    - In *both* cases the sensitive-path blocklist applies, so a workspace of
      $HOME (or any broad root) never grants access to ~/.ssh, north's home, /etc…

    Pass ``contained=False`` for a read, and for a write the approval layer
    rules on by the task's folder (`write_file`): the folder is a boundary for
    writes, decided in one place (CODING_STYLE §16.1.1), not a wall that makes
    "read my notes on the Desktop" fail.

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
    if contained and workspace and not (_in_handoff_root(str(candidate)) or _in_personal_subdir(str(candidate))):
        root = Path(workspace).resolve()
        if not str(candidate).startswith(str(root) + "/") and candidate != root:
            return None

    if sys.platform != "win32" and _is_blocked_path(str(candidate)):
        return None

    return candidate


def scope_refusal(scope: EditAuthorizer | None, path: Path) -> str | None:
    """Return a refusal reason when *scope* forbids editing *path*, else ``None``.

    The single enforcement point shared by every mutating file tool. A ``None``
    scope means the server supplied no task edit-scope for this run, so the edit
    proceeds exactly as before - preserving the behavior of current public
    callers that never set one. When a scope *is* present it is consulted before
    any bytes are written, and its verdict (permit or a human-readable reason) is
    returned verbatim. The scope object is server-owned and reaches the tool via
    ``ToolInput.edit_scope``, never through the model-controlled ``params``.
    """
    if scope is None:
        return None
    return scope.authorize(path)


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
