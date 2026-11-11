"""macOS Seatbelt confinement for the bash tool (#37).

The kernel, not a reading of the command string, decides what a command may
touch. Two profiles:

- **read-only** - no writes outside `/dev`, no network, no reads of credential
  directories. A command that finishes under it is *proved* to have changed
  nothing, so it needs no card.
- **workspace** - writes limited to the workspace, any folder you approved
  for that one command, and the temp and cache directories tools need;
  credential directories stay unreadable and unwritable. Used for commands you
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

from tools._path import is_north_scratch

_SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Home-relative directories that hold credentials. Unreadable under both profiles.
_SECRET_DIRS = (".ssh", ".aws", ".gnupg", ".config", ".north")

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
    if workspace and is_north_scratch(Path(workspace)):
        operations = "file-read* file-write*" if writable else "file-read*"
        rules.append(f"(allow {operations} (subpath {_quote(workspace)}))")
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
