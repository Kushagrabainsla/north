"""Each tool north writes gets its own Python environment, with the packages it names.

A tool names its packages at the top of its file, in Python's standard inline script
metadata (PEP 723, https://peps.python.org/pep-0723/):

    # /// script
    # dependencies = ["python-kasa==0.12.0"]
    # ///

Each must be pinned to one version, so the approval card shows exactly what is installed.
`uv` makes the environment in north's home (`learned/envs/<tool>-<key>`), with those
packages and the MCP SDK the runner needs, and north's own environment never changes: a
package that needs another version of something north uses cannot break north, two tools
can need different versions, and deleting a tool deletes its environment.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import sys
import tomllib
from importlib import metadata
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement

logger = logging.getLogger(__name__)

# PEP 723's reference expression for a metadata block.
_BLOCK = re.compile(r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$")
_READY = ".ready"
INSTALL_TIMEOUT_SECONDS = 300
# What the runner itself needs, at the version north uses: the MCP SDK.
_RUNNER_PACKAGES = ("mcp",)


class ToolEnvironmentError(RuntimeError):
    """A tool's environment could not be made, with what to do about it."""


def script_dependencies(source: str) -> list[str]:
    """The packages *source* names in its `# /// script` block; [] when it has none.

    Raises ValueError for a block that is not valid TOML or names its packages wrongly.
    """
    blocks = [match for match in _BLOCK.finditer(source) if match.group("type") == "script"]
    if not blocks:
        return []
    if len(blocks) > 1:
        raise ValueError("The file has two `# /// script` blocks; keep one.")
    content = "".join(
        line[2:] if line.startswith("# ") else line[1:] for line in blocks[0].group("content").splitlines(keepends=True)
    )
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"The `# /// script` block is not valid TOML: {exc}") from None
    dependencies = data.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
        raise ValueError('`dependencies` must be a list of strings, such as ["python-kasa==0.12.0"].')
    return dependencies


def with_dependencies(source: str, packages: list[str]) -> str:
    """*source* with its `# /// script` block saying *packages*, replacing any block it had."""
    stripped = _BLOCK.sub("", source).lstrip("\n")
    if not packages:
        return stripped
    lines = ["# /// script", f"# dependencies = {json.dumps(packages)}", "# ///", ""]
    return "\n".join(lines) + "\n" + stripped


def pin_problems(packages: list[str]) -> list[str]:
    """Why north will not install *packages*; [] when each is one package at one exact version."""
    problems = []
    for text in packages:
        try:
            requirement = Requirement(text)
        except InvalidRequirement as exc:
            problems.append(f"{text!r} is not a package requirement: {exc}.")
            continue
        if requirement.url:
            problems.append(f"{text!r} installs from a URL; name a package on PyPI, such as 'python-kasa==0.12.0'.")
            continue
        pins = [spec for spec in requirement.specifier if spec.operator == "=="]
        if len(pins) != 1 or len(requirement.specifier) != 1 or "*" in pins[0].version:
            problems.append(
                f"{text!r} must name one exact version, such as '{requirement.name}==1.2.3', so the approval "
                "card shows exactly what is installed."
            )
    return problems


def runner_packages() -> list[str]:
    """The packages every tool environment has for the runner, at the versions north itself uses."""
    return [f"{name}=={metadata.version(name)}" for name in _RUNNER_PACKAGES]


def find_uv() -> str | None:
    """The `uv` program: on PATH, where its installer puts it, or beside this Python."""
    candidates = [
        shutil.which("uv"),
        str(Path.home() / ".local" / "bin" / "uv"),
        str(Path(sys.executable).parent / "uv"),
    ]
    return next((path for path in candidates if path and Path(path).is_file()), None)


class ToolEnvironments:
    """The environments of the tools north wrote, one per tool and set of packages, under *root*."""

    def __init__(self, root: Path, uv: str | None = None) -> None:
        self.root = root
        self._uv = uv
        self._locks: dict[str, asyncio.Lock] = {}

    def path_for(self, tool: str, packages: list[str]) -> Path:
        python = f"{sys.version_info.major}.{sys.version_info.minor}"
        key = hashlib.sha256(json.dumps([python, *runner_packages(), *sorted(packages)]).encode()).hexdigest()[:12]
        return self.root / f"{tool}-{key}"

    async def python_for(self, tool: str, packages: list[str]) -> Path:
        """The Python of *tool*'s environment with *packages*, made first if it is not there yet."""
        if problems := pin_problems(packages):
            raise ToolEnvironmentError(" ".join(problems))
        path = self.path_for(tool, packages)
        python = path / "bin" / "python"
        async with self._locks.setdefault(tool, asyncio.Lock()):
            if (path / _READY).exists():
                return python
            uv = self._uv or find_uv()
            if uv is None:
                raise ToolEnvironmentError(
                    "North needs uv to give a tool its own packages, and cannot find it. Install it from "
                    "https://docs.astral.sh/uv/ and start north again."
                )
            shutil.rmtree(path, ignore_errors=True)
            version = f"{sys.version_info.major}.{sys.version_info.minor}"
            await _run([uv, "venv", "--quiet", "--python", version, str(path)], "make the environment")
            try:
                await _run(
                    [uv, "pip", "install", "--quiet", "--python", str(python), *runner_packages(), *packages],
                    "install " + (", ".join(packages) or "the runner"),
                )
            except ToolEnvironmentError:
                shutil.rmtree(path, ignore_errors=True)
                raise
            (path / _READY).write_text(json.dumps({"packages": packages}), encoding="utf-8")
            for old in self.root.glob(f"{tool}-*"):
                if old != path:
                    shutil.rmtree(old, ignore_errors=True)
            logger.info("Tool %s: environment ready with %s", tool, ", ".join(packages) or "no packages")
        return python

    def remove(self, tool: str) -> None:
        """Delete every environment of *tool*."""
        for path in self.root.glob(f"{tool}-*"):
            shutil.rmtree(path, ignore_errors=True)


async def _run(argv: list[str], doing: str) -> None:
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), INSTALL_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        raise ToolEnvironmentError(f"uv did not {doing} within {INSTALL_TIMEOUT_SECONDS} s.") from None
    if process.returncode != 0:
        tail = stderr.decode(errors="replace").strip().splitlines()[-6:]
        raise ToolEnvironmentError(f"uv could not {doing}: " + " ".join(tail))
