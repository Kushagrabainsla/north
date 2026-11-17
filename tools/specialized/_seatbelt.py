"""macOS Seatbelt confinement for the bash tool (#37).

The kernel, not a reading of the command string, decides what a command may
touch. Two profiles:

- **read-only** - no writes outside `/dev`, no network, no reads of credential
  directories or the keychain. A command that finishes under it is *proved* to have changed
  nothing, so it needs no card.
- **workspace** - writes limited to the workspace, any folder you approved
  for that one command, and the temp and cache directories tools need;
  credential directories stay unreadable and unwritable, and git hooks, git's
  config and shell start-up files unwritable. Used for commands you
  approved. Given an egress proxy port, the network is closed too, except
  loopback (a project's own tests start local servers) - and the proxy on
  loopback is the only way out (see `_egress.py`).

A task's own folder lives inside `~/.north`, which is otherwise closed, so when
it is the workspace it is opened again for that command - that folder only.

`sandbox-exec` is deprecated by Apple with no replacement; it is what Claude
Code and Codex use on macOS. Only one sandbox layer may be active: a command
already inside a restrictive Seatbelt profile cannot start another
(`sandbox_apply: Operation not permitted`), so an agent started from north
must have its own sandbox off.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from tools._path import DB_SUFFIXES, SECRET_FILENAMES, SENSITIVE_DIR_NAMES, is_north_scratch, north_readable_dirs

_SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Home-relative directories that hold credentials, unreadable under both profiles. The same
# list north's file policy uses (`tools/_path.py`), so the two cannot disagree.
_SECRET_DIRS = tuple(sorted(SENSITIVE_DIR_NAMES))

# Files whose change runs code later, outside any sandbox: git hooks, git's config (which can
# name a hook folder or a program git runs) and shell start-up files. No command writes them,
# wherever they are; write_file can, with the change on its card. The list Anthropic's
# sandbox-runtime protects.
_PROTECTED = (
    r"/\.git/hooks(/|$)",
    r"/\.git/config$",
    r"/\.(bashrc|bash_profile|zshrc|zprofile|profile|gitconfig)$",
)

# The services through which a program reads the macOS keychain.
_KEYCHAIN_SERVICES = ("com.apple.SecurityServer", "com.apple.securityd", "com.apple.securityd.xpc")

# Home-relative caches that package managers and compilers write to.
_CACHE_DIRS = (".cache", ".npm", ".cargo", ".rustup", ".gradle", ".m2", "Library/Caches", ".local/share/uv")

# What tools generate wherever they run, and no project keeps as code. Other ignored paths are asked of git.
_GENERATED_NAMES = (
    "__pycache__",
    "node_modules",
    ".venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".next",
    ".turbo",
)

_TEMP_DIRS = ("/private/tmp", "/private/var/folders")
_WRITABLE_DEVICES = ("/dev/null", "/dev/tty", "/dev/dtracehelper", "/dev/zero")

# What a kernel denial looks like in a command's stderr.
DENIAL_MARKERS = (
    "operation not permitted",
    "read-only file system",
    "could not resolve host",
    "nodename nor servname",
    "network is unreachable",
)


@dataclass(frozen=True)
class RepositoryLimit:
    """A git repository a command may not write into, except git's own folder and what git ignores."""

    root: str
    ignored: tuple[str, ...] = ()


def _regex_literal(path: str) -> str:
    return re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", path)


def _repository_rules(limit: RepositoryLimit) -> list[str]:
    """Close the repository, then open what is not its code. Decided by the kernel, whatever program writes."""
    root = os.path.realpath(limit.root)
    kept = " ".join(f"(subpath {_quote(os.path.join(root, name))})" for name in (".git", *limit.ignored))
    generated = "|".join(_regex_literal(name) for name in _GENERATED_NAMES)
    return [
        f"(deny file-write* (subpath {_quote(root)}))",
        f'(allow file-write* {kept} (regex #"^{_regex_literal(root)}/(.*/)?({generated})(/|$)"))',
    ]


# A path a refusal names: "rm: x.dmg: Operation not permitted", "open /a/b: operation not permitted".
_REFUSED_PATH = re.compile(r"([^\s:'\"`]+)['\"`]?:\s*(?:operation not permitted|read-only file system)", re.IGNORECASE)
# A unix socket a command tried: "unix:///Users/me/.docker/run/docker.sock", "/var/run/x.sock".
_UNIX_SOCKET = re.compile(r"(?:unix://)?(/[^\s:'\"`%/][^\s:'\"`%]*\.sock)\b")
_NETWORK_MARKERS = ("could not resolve host", "nodename nor servname", "network is unreachable")
_NOT_MACOS = (
    "North's own sandbox refused part of this command. This is not a macOS permission: "
    "Full Disk Access or another macOS setting will not change it."
)


def explain(output: str, workspace: str | None, *, also_writable: tuple[str, ...] = ()) -> str:
    """What north's sandbox refused in *output*, and what can be done; "" when it shows no refusal.

    North built the box, so it compares each refused path with what the box allows,
    instead of letting the model guess. Told only "Operation not permitted", north
    blamed macOS and told the user to grant Full Disk Access for deletes in
    ~/Downloads and a call to the Docker socket (issue #82, item 8).
    """
    lowered = output.lower()
    sockets = sorted(set(_UNIX_SOCKET.findall(output)))
    if not denied(output) and not sockets:
        return ""
    refused = [Path(found).expanduser() for found in dict.fromkeys(_REFUSED_PATH.findall(output))]
    # A relative path is relative to a `cd` north cannot see; the general rule below covers it.
    reasons = [
        reason for path in refused if path.is_absolute() and (reason := _why_refused(path, workspace, also_writable))
    ]
    reasons += [
        f"`{socket}` is a unix socket. Unix sockets, such as the Docker daemon's, are closed to every "
        "command. No answer opens them: if the user needs it, the user runs the command."
        for socket in sockets
    ]
    if any(marker in lowered for marker in _NETWORK_MARKERS):
        reasons.append(
            "A command reaches the network only through north's proxy (HTTP_PROXY), to the hosts it allows. "
            "A program that does not use the proxy cannot connect."
        )
    if reasons:
        return "\n".join([_NOT_MACOS, *(f"- {reason}" for reason in reasons)])
    if refused and all(path.is_absolute() for path in refused):
        return ""  # every refused path is one the box allows: something other than north refused it
    granted = [f"`{os.path.realpath(folder)}`" for folder in (workspace, *also_writable) if folder]
    where = ", ".join([*granted, "the temp and cache folders"])
    return (
        f"If this command wrote outside {where}, north's own sandbox refused it, not macOS: "
        "Full Disk Access will not change it. To write elsewhere, call bash again with "
        "write_outside=[that folder]; the user is asked first. Git hooks, git's config and shell start-up "
        "files (.zshrc, .bashrc, .gitconfig) are never writable from a command: change one with write_file."
    )


def _writable_roots(workspace: str | None, also_writable: tuple[str, ...]) -> list[str]:
    """The folders the workspace profile lets a command write, as real paths."""
    folders = [folder for folder in (workspace, *also_writable) if folder]
    folders += [*_TEMP_DIRS, *(str(Path.home() / name) for name in _CACHE_DIRS)]
    return [os.path.realpath(folder) for folder in folders]


def _within(path: str, folders: list[str] | tuple[str, ...]) -> bool:
    return any(path == folder or path.startswith(folder.rstrip("/") + "/") for folder in folders)


def _why_refused(path: Path, workspace: str | None, also_writable: tuple[str, ...]) -> str:
    """Why the workspace profile refuses *path*, or "" when it allows it (then north did not refuse it)."""
    from config.settings import settings

    real = os.path.realpath(path)
    north_home = os.path.realpath(settings.north_home)
    credentials = [os.path.realpath(p) for p in _secret_dirs() if os.path.realpath(p) != north_home]
    keychains = [os.path.realpath(Path.home() / "Library" / "Keychains"), "/Library/Keychains"]
    if _within(real, credentials + keychains) or Path(real).name in SECRET_FILENAMES:
        return f"`{path}` holds credentials, which are closed to every command. No answer opens it."
    if _within(real, [north_home]) and not (workspace and _within(real, [os.path.realpath(workspace)])):
        if real.endswith(DB_SUFFIXES) or not _within(real, [os.path.realpath(p) for p in north_readable_dirs()]):
            return f"`{path}` is north's own data, which is closed to every command. No answer opens it."
        return (
            f"`{path}` is in north's own folder: a command may read it but not write it. "
            "Write north's notes with write_file."
        )
    if any(re.search(pattern, real) for pattern in _PROTECTED):
        return (
            f"`{path}` is a git hook, git's config or a shell start-up file: never writable from a command. "
            "Change it with write_file."
        )
    if _within(real, _writable_roots(workspace, also_writable)):
        return ""
    folder = real if Path(real).is_dir() else os.path.dirname(real)
    return (
        f"`{path}` is outside the folders this command may write. To change it, call bash again with "
        f'write_outside=["{folder}"]; the user is asked first.'
    )


def available() -> bool:
    """True when this host has Seatbelt."""
    return sys.platform == "darwin" and shutil.which("sandbox-exec") is not None


def denied(stderr: str) -> bool:
    """True when *stderr* shows the sandbox refused a write, a read or the network."""
    lowered = stderr.lower()
    return any(marker in lowered for marker in DENIAL_MARKERS)


class Seatbelt:
    """The macOS `OsSandbox` (see `_os_sandbox.py`)."""

    @staticmethod
    def available() -> bool:
        return available()

    @staticmethod
    def wrap(
        command: str,
        workspace: str | None,
        *,
        writable: bool,
        proxy_port: int | None = None,
        also_writable: tuple[str, ...] = (),
        scratch: str | None = None,
        repository: RepositoryLimit | None = None,
    ) -> list[str]:
        return wrap(
            command,
            workspace,
            writable=writable,
            proxy_port=proxy_port,
            also_writable=also_writable,
            scratch=scratch,
            repository=repository,
        )

    @staticmethod
    def denied(stderr: str) -> bool:
        return denied(stderr)

    @staticmethod
    def explain(output: str, workspace: str | None, *, also_writable: tuple[str, ...] = ()) -> str:
        return explain(output, workspace, also_writable=also_writable)


def _quote(path: str | Path) -> str:
    resolved = os.path.realpath(path)
    return '"' + resolved.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _secret_dirs() -> list[str]:
    from config.settings import settings

    home = Path.home()
    paths = [str(home / name) for name in _SECRET_DIRS]
    if str(settings.north_home) not in paths:
        paths.append(str(settings.north_home))
    return paths


def _closed_files() -> str:
    """North's databases inside its home, and secret files anywhere, as Seatbelt filters."""
    from config.settings import settings

    home = _regex_literal(os.path.realpath(settings.north_home))
    suffixes = "|".join(_regex_literal(suffix) for suffix in DB_SUFFIXES)
    names = "|".join(_regex_literal(name) for name in sorted(SECRET_FILENAMES))
    return f'(regex #"^{home}/.*({suffixes})$") (regex #"/({names})$")'


def profile(
    workspace: str | None,
    *,
    writable: bool,
    proxy_port: int | None = None,
    also_writable: tuple[str, ...] = (),
    scratch: str | None = None,
    repository: RepositoryLimit | None = None,
) -> str:
    """The Seatbelt profile for a command run in *workspace*.

    Later rules win, so each profile denies broadly and then allows narrowly.
    Paths are resolved first: the kernel matches real paths, and `/tmp` and
    `/var` are symlinks on macOS. *also_writable* are folders outside the
    workspace you approved this command writing to. *scratch* is a throwaway
    folder, writable even read-only: what lands there is deleted afterwards, so
    it changes nothing. *repository* closes a git repository to the command.
    """
    rules = ["(version 1)", "(allow default)", "(deny file-write*)"]
    allowed = [_quote(p) for p in _TEMP_DIRS]
    if writable:
        home = Path.home()
        allowed += [_quote(home / name) for name in _CACHE_DIRS]
        allowed += [_quote(folder) for folder in (workspace, *also_writable) if folder]
    devices = " ".join(f"(literal {_quote(d)})" for d in _WRITABLE_DEVICES)
    rules.append(f'(allow file-write* {devices} (regex #"^/dev/ttys[0-9]+$"))')
    if allowed and writable:
        rules.append("(allow file-write* " + " ".join(f"(subpath {p})" for p in allowed) + ")")
    if scratch:
        rules.append(f"(allow file-write* (subpath {_quote(scratch)}))")
    if repository and writable:
        rules += _repository_rules(repository)
    secrets = " ".join(f"(subpath {_quote(p)})" for p in _secret_dirs())
    # Unwritable as well as unreadable: a workspace of the home folder used to
    # let an approved command write into ~/.ssh.
    rules.append(f"(deny file-read* file-write* {secrets})")
    # The keychain too. Open, a command that needed no card read gh's GitHub token with
    # `security find-generic-password` (measured 2026-11-16). Certificate checks use another
    # service, so HTTPS still works; a credential stored in the keychain does not.
    keychain_dirs = (Path.home() / "Library" / "Keychains", "/Library/Keychains")
    keychains = " ".join(f"(subpath {_quote(p)})" for p in keychain_dirs)
    rules.append(f"(deny file-read* file-write* {keychains})")
    rules.append("(deny mach-lookup " + " ".join(f'(global-name "{name}")' for name in _KEYCHAIN_SERVICES) + ")")
    # North's notes, news and task folders are readable, as they are to north's own
    # file policy: with no read tool left, the shell is how earlier notes are read back.
    readable = " ".join(f"(subpath {_quote(p)})" for p in north_readable_dirs())
    rules.append(f"(allow file-read* {readable})")
    if workspace and is_north_scratch(Path(workspace)):
        operations = "file-read* file-write*" if writable else "file-read*"
        rules.append(f"(allow {operations} (subpath {_quote(workspace)}))")
    # Whatever was opened above, north's databases and secret files stay closed.
    rules.append(f"(deny file-read* file-write* {_closed_files()})")
    if writable:
        rules.append("(deny file-write* " + " ".join(f'(regex #"{pattern}")' for pattern in _PROTECTED) + ")")
    if not writable:
        rules.append("(deny network*)")
    elif proxy_port is not None:
        # No unix sockets: the DNS resolver and the Docker socket are both one.
        # Outbound and inbound stay separate rules: `local ip "localhost:*"` on
        # `network*` also matches an outbound connection to anywhere.
        rules.append("(deny network*)")
        rules.append('(allow network-outbound (remote ip "localhost:*"))')
        rules.append('(allow network-bind network-inbound (local ip "localhost:*"))')
    return "\n".join(rules)


def _proxy_env(proxy_port: int) -> list[str]:
    """Point every program that honours the usual variables at the egress proxy."""
    url = f"http://127.0.0.1:{proxy_port}"
    names = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    no_proxy = "localhost,127.0.0.1,::1"
    return [*(f"{name}={url}" for name in names), f"NO_PROXY={no_proxy}", f"no_proxy={no_proxy}"]


def wrap(
    command: str,
    workspace: str | None,
    *,
    writable: bool,
    proxy_port: int | None = None,
    also_writable: tuple[str, ...] = (),
    scratch: str | None = None,
    repository: RepositoryLimit | None = None,
) -> list[str]:
    """The argv that runs *command* under the chosen profile."""
    argv = [
        _SANDBOX_EXEC,
        "-p",
        profile(
            workspace,
            writable=writable,
            proxy_port=proxy_port,
            also_writable=also_writable,
            scratch=scratch,
            repository=repository,
        ),
    ]
    env = _proxy_env(proxy_port) if writable and proxy_port is not None else []
    if scratch:
        env.append(f"TMPDIR={scratch}")
    if env:
        argv += ["/usr/bin/env", *env]
    return [*argv, "/bin/sh", "-c", command]
