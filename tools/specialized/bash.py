"""BashTool - run shell commands inside the workspace.

Every command is described to the approval layer before the subprocess is
spawned (see `Tool.execute`). The sandbox setting is injected at startup (see
orchestrator/app.py), so this tool is registered manually.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

import asyncio
import functools
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from approval.approvals import Request
from approval.policy import Action, ActionKind
from coding_agents.constants import VERIFY_TIMEOUT_SECONDS
from tools._path import (
    deletes_a_root,
    edited_paths,
    git_repository,
    is_sensitive_path,
    named_paths,
    personal_dir,
    simple_commands,
)
from tools.base import Tool, prepared
from tools.models import ToolInput, ToolOutput
from tools.specialized import _os_sandbox
from tools.specialized._egress import EgressProxy
from tools.specialized._os_sandbox import RepositoryLimit
from tools.specialized._sandbox import (
    SandboxConfig,
    build_run_argv,
    docker_available,
)
from utils.edit_scope import narrow_workspace
from utils.handoff import task_of_handoff_dir
from utils.processes import stop_process_group

_TIMEOUT = 30
_MAX_TIMEOUT = 300
# Output is not cut here. The agent keeps a long result whole and shows the model a
# part of it, with a handle to read the rest (`read_tool_output`). A cut here at
# 30,000 characters lost the rest before that store ever saw it (issue #78).
_REFUSAL_TAIL = 2_000
_GIT_TIMEOUT = 20
# Each ignored path is a sandbox rule, and the kernel takes about 2 ms to compile one.
_MAX_IGNORED_PATHS = 500
# Programs that may write in a repository: git is how a change is committed and shipped, and `cd` only moves.
_REPOSITORY_COMMANDS = frozenset({"git", "gh", "cd"})
# Build folders that may not exist yet, kept writable when the project ignores them.
_BUILD_OUTPUTS = ("build", "dist", "target", "out", "coverage", ".coverage", "htmlcov", ".gradle")
_PLAIN_IGNORE_ENTRY = re.compile(r"/?[\w.-]+/?")
_IF_REFUSED_HERE = "something printed a refusal. If it was a write in this folder, it did not happen. "
_REPOSITORY_READ_ONLY = (
    "`{root}` is a git repository, and north's own commands cannot write its files (only what git ignores). "
    "To change code there, hand the work to coding_agent."
)

# These patterns are caught as a UX convenience - NOT a security boundary.
# The approval gate above is the actual guard: the user sees the exact command
# and decides. This list only catches the most obviously destructive typos.
# Do not rely on it to stop a determined attacker or a misbehaving model  -
# any determined bypass (extra spaces, equivalent syntax) will get through.
_OBVIOUS_DESTRUCTIVE_HINTS = [
    ":(){ :|:& };:",
    "dd if=",
    "> /dev/sd",
]


def _boundary(input: ToolInput) -> str | None:
    """Where a command may write: the folder the server granted, or a subfolder of it the model chose.

    Never the model's `workspace` alone (CODING_STYLE §16.1.1). With nothing
    granted there is no boundary, and nothing but temp and caches is writable.
    """
    return narrow_workspace(input.granted_workspace or "", str(input.params.get("workspace") or "")) or None


def _task_notes(boundary: str | None) -> str | None:
    """The notes folder of the task whose own folder *boundary* is, or None when it is not a task's folder.

    A task has two folders of its own: `tasks/<id>` and `notes/<id>`, where its prompt tells
    it to keep notes. A command in the contained box may write to both, and to no other
    task's: it may read another task's folders, not change them.
    """
    task_id = task_of_handoff_dir(boundary) if boundary else None
    return f"{personal_dir('notes')}/{task_id}" if task_id else None


def _cwd(input: ToolInput, boundary: str | None) -> str | None:
    """Where the command starts. Not a limit on anything: the sandbox is."""
    return str(input.params.get("workspace") or "") or boundary


def _outside_folders(input: ToolInput) -> tuple[str, ...] | ToolOutput:
    """The folders outside the boundary the command asked to write to, resolved.

    A credential folder is refused here, before any card: no answer makes it writable.
    """
    raw = input.params.get("write_outside") or []
    boundary = _boundary(input)
    root = Path(boundary).resolve() if boundary else None
    folders: list[str] = []
    for item in [raw] if isinstance(raw, str) else raw:
        folder = Path(str(item)).expanduser().resolve()
        if is_sensitive_path(folder):
            return ToolOutput(
                success=False,
                error=f"`{folder}` holds credentials; no command may write there.",
                failure_kind="refused",
            )
        if root is None or not folder.is_relative_to(root):
            folders.append(str(folder))
    return tuple(dict.fromkeys(folders))


def _edited_repository(command: str, cwd: str | None) -> str:
    """The git repository a file this command recognisably edits is in, or ""."""
    for path in edited_paths(command, cwd):
        if (repository := git_repository(path)) is not None:
            return str(repository)
    return ""


async def _git_lines(root: Path, *args: str) -> list[str]:
    """The lines git prints, or none if it cannot be asked: then only the fixed generated names stay writable."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(root),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT)
    except (OSError, TimeoutError):
        return []
    return [line.rstrip("/") for line in stdout.decode("utf-8", errors="replace").splitlines() if line]


async def _repository_limit(command: str, where: str | None) -> RepositoryLimit | None:
    """The repository *command* must not write into, or None when it runs outside one or is git itself.

    North's own tools do not edit a repository (CODING_STYLE §16.1); reading a command cannot tell what it
    writes, so the sandbox holds that. Git keeps working: committing and shipping are how a change leaves.
    """
    root = git_repository(Path(where)) if where else None
    if root is None or all(words and words[0] in _REPOSITORY_COMMANDS for words in simple_commands(command)):
        return None
    ignored = await _git_lines(root, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory")
    names = (*_BUILD_OUTPUTS, *_plain_ignore_entries(root))
    # Both forms: a pattern like `dist/` matches only a folder, and these may not exist yet.
    future = await _git_lines(root, "check-ignore", *names, *(f"{name}/" for name in names))
    return RepositoryLimit(str(root), tuple(dict.fromkeys([*future, *ignored]))[:_MAX_IGNORED_PATHS])


def _plain_ignore_entries(root: Path) -> list[str]:
    """The plain names in the repository's `.gitignore`: output a build may create that is not there yet."""
    try:
        lines = (root / ".gitignore").read_text(errors="replace").splitlines()
    except OSError:
        return []
    return [line.strip().strip("/") for line in lines if _PLAIN_IGNORE_ENTRY.fullmatch(line.strip())]


def _all_output(output: ToolOutput) -> str:
    return f"{output.error or ''}\n{output.data.get('stderr', '')}\n{output.data.get('stdout', '')}"


@functools.cache
def _north_commands() -> str:
    """A folder with north's own commands, put first on every command's PATH.

    It holds `north` (so `north text file.pdf` works however north was started: a
    source checkout has no `north` launcher) and `rg`, which north installs with
    itself. Only these two: north's whole Python folder would also shadow the
    user's own `python3`. This `north` offers `text` alone (`cli/text.py`): the
    full CLI loads settings from north's home, which no command may read.
    """
    folder = Path(tempfile.mkdtemp(prefix="north-commands-"))
    package_root = Path(__file__).resolve().parents[2]
    launcher = folder / "north"
    launcher.write_text(
        f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(package_root)!r})\n"
        "from cli.text import main\nsys.exit(main(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    ripgrep = Path(sys.executable).parent / "rg"
    if ripgrep.exists():
        (folder / "rg").symlink_to(ripgrep)
    return str(folder)


def _environment() -> dict[str, str]:
    return {**os.environ, "PATH": os.pathsep.join([_north_commands(), os.environ.get("PATH", "")])}


def _timeout(input: ToolInput) -> int:
    """How long the command may run: what the call asked for, within the limit, else the default.

    The read-only run that may skip the card gets the same time. With 10 seconds, a slow
    read such as a `find` over a large folder went to a card although it changed nothing.
    """
    raw = input.params.get("timeout")
    # North's own test run in a copy may take as long as a project's suite needs; a model's command may not.
    longest = VERIFY_TIMEOUT_SECONDS if input.isolated_copy else _MAX_TIMEOUT
    try:
        return min(max(int(raw), 1), longest) if raw is not None else _TIMEOUT
    except (ValueError, TypeError):
        return _TIMEOUT


class BashTool(Tool):
    """Runs a shell command and returns stdout, stderr, and return code.

    A command that changes something needs approval before it executes - the card
    shows the exact command string so the user sees precisely what will run. With
    the OS sandbox on (macOS), a command is first tried where the kernel refuses
    every write and the network; one that finishes there changed nothing and
    needs no card, and an approved one runs with writes limited to the task's
    folder, plus any folder outside it named in `write_outside` and shown on its
    card (CODING_STYLE §16.1.1).
    """

    name = "bash"
    is_mutating = True
    description = (
        "Run a shell command and return stdout/stderr/returncode. This is how you read and search files:"
        " `cat -n file` (with line numbers), `sed -n '120,180p' file` (a range of lines),"
        " `ls -la dir`, `find dir -name '*.md'`, `rg -n 'pattern' dir` or `grep -rn 'pattern' dir`,"
        " and `north text file.pdf` for the text of a PDF."
        " Commands that only read run at once, and so do commands that write only to this task's own folders"
        " (its task folder and its notes folder) and temp, with no network; anything else needs approval."
        " To create or replace a file, use write_file: its card shows the change."
        " Default timeout 30 s, pass timeout= (max 300) for longer commands."
        " A long output is kept whole: you see part of it and can read the rest with read_tool_output."
        " Approved commands reach only allowed hosts (package registries, code hosts);"
        " a 403 on CONNECT means the host is not allowed."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "workspace": {
                "type": "string",
                "description": "Working directory for the command (optional)",
            },
            "write_outside": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Folders outside the task's folder this command must write to, such as ~/Desktop. "
                    "Only when it has to: the user is asked, with these folders shown."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": (
                    "Timeout in seconds (default 30, max 300). "
                    "Use higher values for test suites or long-running builds."
                ),
            },
        },
        "required": ["command"],
    }

    def __init__(
        self,
        sandbox: SandboxConfig | None = None,
        os_sandbox: bool = False,
        allowed_domains: tuple[str, ...] | None = None,
    ) -> None:
        self._sandbox = sandbox or SandboxConfig()
        # One sandbox layer only: Docker, when asked for, replaces Seatbelt.
        self._os_sandbox = _os_sandbox.current() if os_sandbox and not self._sandbox.enabled else None
        self._egress = EgressProxy(allowed_domains) if allowed_domains is not None else EgressProxy()

    def _action(
        self,
        command: str,
        *,
        read_only: bool = False,
        boundary: str | None = None,
        outside: tuple[str, ...] = (),
        cwd: str | None = None,
        ran_contained: bool = False,
    ) -> Action:
        """What this command is, as facts. What that *means* is the policy's call."""
        return Action(
            agent="bash",
            kind=ActionKind.SHELL_COMMAND,
            summary=command,
            command=command,
            # The folders are part of what you approve: the same command writing elsewhere asks again.
            args=" ".join(f"writes={folder}" for folder in outside),
            workspace=boundary or "",
            # The same command in another folder is another action: `rm -rf build` here is not there.
            place=cwd or boundary or "",
            # What a "no" to this command covers for the rest of the task.
            paths=(*named_paths(command, cwd or boundary), *(Path(folder) for folder in outside)),
            edits_repository=_edited_repository(command, cwd or boundary),
            read_only=read_only,
            mutating=not read_only,
            ran_contained=ran_contained,
            leaves_sandbox=bool(outside),
            obviously_destructive=deletes_a_root(command)
            or any(hint in command for hint in _OBVIOUS_DESTRUCTIVE_HINTS),
        )

    def format_output(self, data: dict[str, Any]) -> str:
        return str(data.get("stdout", data.get("output", ""))).strip()

    async def describe(self, input: ToolInput) -> Request | None:
        # Whether a catastrophic pattern is refused, and in which modes, is the
        # policy's call - this tool only reports that it recognises one.
        command = input.params.get("command")
        outside = _outside_folders(input)
        if not command or isinstance(outside, ToolOutput):
            return None
        boundary = _boundary(input)
        cwd = _cwd(input, boundary)
        proved, wrote = await self._prove_read_only(command, boundary, cwd, _timeout(input))
        contained, ran_once = None, False
        if proved is None and wrote and not outside and not _edited_repository(command, cwd or boundary):
            contained, ran_once = await self._run_contained(command, boundary, cwd, _timeout(input))
        also = "\n\nAlso writes to:\n" + "\n".join(f"- `{folder}`" for folder in outside) if outside else ""
        again = (
            "\n\nNorth already ran it once, writing only to this task's own folders and with no network, "
            "and it did not finish there. Approving runs it again in full."
            if ran_once and contained is None
            else ""
        )
        return Request(
            action=self._action(
                command,
                read_only=proved is not None,
                boundary=boundary,
                outside=outside,
                cwd=cwd,
                ran_contained=contained is not None,
            ),
            title="Shell Command - Approval Required",
            message=f"```\n{command}\n```{also}{again}",
            declined="Command cancelled by user.",
            prepared=proved or contained,
        )

    async def _prove_read_only(
        self, command: str, boundary: str | None, cwd: str | None, timeout: int
    ) -> tuple[ToolOutput | None, bool]:
        """Run *command* where the kernel refuses every write and the network.

        Returns its output when it finished successfully without being refused
        anything, which proves it changed nothing - the output is then what `run`
        returns, so the command runs once. Returns None when it was refused, failed
        or took too long: it goes to approval, and runs again if approved. The second
        value says whether the sandbox refused it something - it tried to write.

        A command that merely *failed* proves nothing. The sandbox's refusal does
        not always say so: pytest, refused its scratch file, reports "No usable
        temporary directory found", which no marker recognises, and its failed
        probe was then returned as the command's real result without the command
        ever running where it could write.
        """
        if not self._os_sandbox:
            return None, False
        # A throwaway temp folder: programs need one to start, and what lands in it is deleted here.
        with tempfile.TemporaryDirectory(prefix="north-probe-") as scratch:
            argv = self._os_sandbox.wrap(command, boundary, writable=False, scratch=scratch)
            output = await self._spawn(argv, cwd, timeout, command=command)
        refused = output.data is not None and self._os_sandbox.denied(_all_output(output))
        if output.data is None or output.data.get("returncode") != 0 or refused:
            return None, refused
        return output, False

    async def _run_contained(
        self, command: str, boundary: str | None, cwd: str | None, timeout: int
    ) -> tuple[ToolOutput | None, bool]:
        """Run *command* where it may write only to its task's own folders and temp, with no network.

        For a command the read-only run refused a write: north saving its own notes for this task,
        a script it writes and runs, an image it crops. On a clean finish, nothing outside those
        folders changed, so it needs no approval, as a write_file there needs none; its output is
        what `run` returns, so it runs once. The first value is that output, or None. The second
        says whether it ran at all: a run that was refused or failed goes to approval, and the card
        says it already ran once in the box.

        Only when the command runs in a task's own folder: one in a folder of yours asks, as before.
        """
        notes = _task_notes(boundary)
        if not self._os_sandbox or notes is None:
            return None, False
        argv = self._os_sandbox.wrap(command, boundary, writable=True, also_writable=(notes,), contained=True)
        output = await self._spawn(argv, cwd, timeout, command=command)
        if output.data is None or output.data.get("returncode") != 0 or self._os_sandbox.denied(_all_output(output)):
            return None, True
        return output, True

    async def _resolve_execution(
        self,
        command: str,
        cwd: str | None,
        boundary: str | None,
        outside: tuple[str, ...],
        repository: RepositoryLimit | None = None,
    ) -> tuple[list[str] | None, str | None, str | None]:
        """Decide how to run *command*: (argv, host_cwd, error).

        - No sandbox → (None, cwd, None): run on the host as before.
        - Docker on + available + a folder to mount → (docker argv, None, None).
        - Docker on but missing, or no folder → (None, None, error): fail
          closed, because a requested security control must never silently degrade.
        - OS sandbox on → (seatbelt argv, cwd, None): writes limited to *boundary*
          and the approved *outside* folders. With no boundary, only temp and caches:
          it used to fall back to the folder north was started in.
        """
        if self._os_sandbox:
            port = await self._egress.start()
            argv = self._os_sandbox.wrap(
                command, boundary, writable=True, proxy_port=port, also_writable=outside, repository=repository
            )
            return argv, cwd, None
        if not self._sandbox.enabled:
            return None, cwd, None
        if not boundary:
            return None, None, "Sandboxed execution requires a workspace to mount, but none was provided."
        if not await docker_available():
            return (
                None,
                None,
                "Sandboxed execution is enabled but Docker is unavailable - refusing to run on the host.",
            )
        return build_run_argv(command, boundary, self._sandbox), None, None

    async def aclose(self) -> None:
        await self._egress.stop()

    async def run(self, input: ToolInput) -> ToolOutput:
        command = input.params.get("command")
        if not command:
            return ToolOutput(success=False, error="Parameter 'command' is required.")
        if isinstance(proved := prepared(input), ToolOutput):
            return proved

        outside = _outside_folders(input)
        if isinstance(outside, ToolOutput):
            return outside
        boundary = _boundary(input)
        cwd = _cwd(input, boundary)
        timeout = _timeout(input)

        limit = None
        if self._os_sandbox and not input.isolated_copy:
            limit = await _repository_limit(command, cwd or boundary)
        exec_argv, exec_cwd, sandbox_error = await self._resolve_execution(command, cwd, boundary, outside, limit)
        if sandbox_error is not None:
            return ToolOutput(success=False, error=sandbox_error)
        output = await self._spawn(exec_argv, exec_cwd, timeout, command=command)
        if limit is not None and self._os_sandbox and self._os_sandbox.denied(_all_output(output)):
            note = _REPOSITORY_READ_ONLY.format(root=limit.root)
            if output.success:
                # A passing command can print a refusal that has nothing to do with the repository, and a pipe
                # can hide one that does: it stays a success, and the note covers the second case.
                output.data["stdout"] = f"{output.data.get('stdout', '')}\n[north: {_IF_REFUSED_HERE}{note}]"
                return output
            tail = _all_output(output).strip()[-_REFUSAL_TAIL:]
            return ToolOutput(success=False, failure_kind="refused", data=output.data, error=f"{tail}\n{note}")
        if self._os_sandbox and (
            note := self._os_sandbox.explain(_all_output(output), boundary, also_writable=outside)
        ):
            # Refused, not broken: say it was north's box and how to ask, so the work is not dropped and macOS is
            # not blamed. On success too: `cd ~/Downloads; rm x.dmg; df -h` passes after its rm was refused.
            if output.success:
                output.data["stdout"] = f"{output.data.get('stdout', '')}\n[north: {note}]"
            else:
                output.error = f"{output.error}\n{note}"
        return output

    async def _spawn(self, argv: list[str] | None, cwd: str | None, timeout: int, *, command: str) -> ToolOutput:
        """Run *argv* - or *command* through the host shell when there is none - and collect its output."""
        try:
            if argv is not None:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=_environment(),
                    start_new_session=True,
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=_environment(),
                    start_new_session=True,
                )
        except Exception as exc:
            return ToolOutput(success=False, error=str(exc))
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            return ToolOutput(success=False, error=f"Command timed out after {timeout}s.")
        except Exception as exc:
            return ToolOutput(success=False, error=str(exc))
        finally:
            # A timeout or a cancelled task stops the command and whatever it started.
            await stop_process_group(proc, grace_seconds=1.0)

        stdout, stderr = stdout_b.decode("utf-8", errors="replace"), stderr_b.decode("utf-8", errors="replace")
        success = proc.returncode == 0
        return ToolOutput(
            success=success,
            error=None if success else (stderr.strip() or f"exit code {proc.returncode}"),
            data={
                "stdout": stdout,
                "stderr": stderr,
                "returncode": proc.returncode,
            },
        )
