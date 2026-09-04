"""GitTool - structured git operations for engineering agents.

Safe read-only operations (status, diff, log, show, and listing branches)
execute immediately. Mutating operations (add, commit, push, pull, checkout,
stash, merge, branch create/delete) are gated in code behind a user approval
card - the gate does not rely on the agent's system prompt. Force pushes are
permanently blocked via token-level argument parsing. reset/clean are not
offered as actions at all.

Because the read-only path skips the approval card, its *arguments* are
allowlisted too: git's diff family can write a file anywhere on disk
(`--output=`) or run a configured command (`--ext-diff`), which would step
around both the approval gate and the `tools/_path.py` workspace sandbox.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from approval.mode import ApprovalMode
from approval.unattended import UnattendedPolicy
from tools.base import ApprovalGatedTool
from tools.models import ToolInput, ToolOutput
from tools.specialized._approval import gate_mutating_action
from tools.specialized._subprocess import format_diff_output, run_capture

if TYPE_CHECKING:
    from collections.abc import Callable

    from approval.base import Notifier
    from approval.judgement_filter import JudgementFilter
    from approval.store import ApprovalStore
    from orchestrator.stream import EventStreamManager

_TIMEOUT = 30
# `git log -20` - a count with no option letter in front of it.
_BARE_COUNT = re.compile(r"-\d+")

# Actions that never change repository state. Everything else is mutating and
# requires in-code approval before the subprocess is spawned.
_READONLY_ACTIONS: frozenset[str] = frozenset({"status", "diff", "log", "show"})
# `branch` is read-only only when listing; these flags keep it on the fast path.
_BRANCH_LIST_FLAGS: frozenset[str] = frozenset({
    "-a", "-r", "-v", "-vv", "-l", "--list", "--all", "--show-current",
})
# ...and these put `branch` into list mode, where a positional is a glob pattern
# to filter by rather than a branch name to create. `git branch --list foo*`
# cannot mutate anything; `git branch foo` creates a branch.
_BRANCH_LIST_MODE_FLAGS: frozenset[str] = frozenset({"-l", "--list"})

# Read-only actions skip the approval gate, so their arguments are the one place
# an agent reaches the filesystem unsupervised - and git's diff family can write
# and execute. `git diff --output=<path>` creates a file at any absolute path,
# outside the workspace and outside the `tools/_path.py` sandbox; `--ext-diff`
# runs a diff driver configured in the repo. Flags are therefore allowlisted,
# not blocklisted: a blocklist has to predict every future git option that
# touches the disk, and misses the first one it does not know about.
_SAFE_DIFF_FLAGS: frozenset[str] = frozenset({
    "--stat", "--numstat", "--shortstat", "--summary", "--dirstat", "--raw",
    "--name-only", "--name-status", "--diff-filter", "--relative",
    "-p", "-u", "--patch", "--no-patch", "-s",
    "--cached", "--staged", "--merge-base",
    "-U", "--unified", "--function-context", "-W",
    "-w", "-b", "--ignore-all-space", "--ignore-space-change",
    "--ignore-blank-lines", "--ignore-cr-at-eol",
    "--color", "--no-color", "--word-diff", "--color-words",
    "-M", "-C", "--find-renames", "--find-copies", "--find-copies-harder",
    "-R", "--text", "--binary", "--full-index", "--abbrev",
    "--src-prefix", "--dst-prefix", "--no-prefix",
    # Explicitly safe counterparts of the two options that can run a command.
    "--no-ext-diff", "--no-textconv",
})
_SAFE_LOG_FLAGS: frozenset[str] = frozenset({
    "--oneline", "--graph", "--decorate", "--no-decorate",
    "--all", "--branches", "--tags", "--remotes",
    "--first-parent", "--merges", "--no-merges",
    "--reverse", "--topo-order", "--date-order", "--author-date-order",
    "-n", "--max-count", "--skip",
    "--since", "--after", "--until", "--before",
    "--author", "--committer", "--grep", "--regexp-ignore-case", "-i",
    "--follow", "--date", "--pretty", "--format",
    "--abbrev-commit", "--no-abbrev-commit",
})
# `status` builds its own fixed argument list and drops whatever the agent
# passed, so it has no user-controlled flags to check.
_SAFE_FLAGS_BY_ACTION: dict[str, frozenset[str]] = {
    "diff": _SAFE_DIFF_FLAGS,
    "log": _SAFE_LOG_FLAGS | _SAFE_DIFF_FLAGS,
    "show": _SAFE_LOG_FLAGS | _SAFE_DIFF_FLAGS,
}


def _is_force_flag(token: str) -> bool:
    """Token-level force detection - robust against `--force-with-lease=ref` etc."""
    return token == "-f" or token.startswith("--force")


def _flag_allowed(token: str, allowed: frozenset[str]) -> bool:
    """True when *token* names an option on the read-only allowlist.

    Handles the three shapes git accepts for one option: `--unified=3`,
    `-U3` (value attached to a short flag), and `-20` (a bare count for `log`).
    """
    name = token.split("=", 1)[0]
    if name in allowed:
        return True
    if len(name) > 2 and not name.startswith("--") and name[:2] in allowed:
        return True
    return bool(_BARE_COUNT.fullmatch(name))


def _unsafe_flags(action: str, args: list[str]) -> list[str]:
    """Options in *args* that are not allowlisted for a read-only *action*."""
    allowed = _SAFE_FLAGS_BY_ACTION.get(action)
    if allowed is None:
        return []
    unsafe: list[str] = []
    for token in args:
        if token == "--":
            break  # everything after the separator is a pathspec, not an option
        if token.startswith("-") and not _flag_allowed(token, allowed):
            unsafe.append(token)
    return unsafe


class GitTool(ApprovalGatedTool):
    """Run git commands with structured output and safety guards."""

    name = "git"
    is_mutating = True
    description = (
        "Run git operations in the workspace. "
        "Read-only actions (status, diff, log, show, branch listing) execute immediately. "
        "Mutating actions (add, commit, push, pull, checkout, stash, merge, branch create/delete) "
        "automatically show the user an approval card before running - no separate "
        "request_approval call is needed. Force-push is always blocked; reset/clean are not available."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "status",
                    "diff",
                    "log",
                    "branch",
                    "show",
                    "add",
                    "commit",
                    "push",
                    "pull",
                    "checkout",
                    "stash",
                    "merge",
                ],
                "description": "Git action to perform",
            },
            "args": {
                "type": "string",
                "description": (
                    "Extra arguments passed to the git command. "
                    "For commit: the commit message string. "
                    "For add: paths to stage (default '.'). "
                    "For checkout: branch name. "
                    "For diff/log/show: optional path or ref."
                ),
                "default": "",
            },
            "workspace": {
                "type": "string",
                "description": "Repository root directory (defaults to CWD)",
            },
        },
        "required": ["action"],
    }

    def format_output(self, data: dict[str, Any]) -> str:
        stdout = str(data.get("stdout", "")).strip()
        command = str(data.get("command", ""))
        if "diff" in command:
            return format_diff_output(stdout)
        return stdout

    def __init__(
        self,
        approval_store: ApprovalStore | None = None,
        stream_manager: EventStreamManager | None = None,
        approval_timeout_seconds: float = 300.0,
        judgement_filter: JudgementFilter | None = None,
        notifier: Notifier | None = None,
        unattended: UnattendedPolicy | None = None,
        mode_provider: Callable[[], ApprovalMode] | None = None,
    ) -> None:
        super().__init__(approval_store, stream_manager, approval_timeout_seconds, judgement_filter, notifier)
        self._unattended = unattended or UnattendedPolicy()
        self._mode_provider = mode_provider

    def _mode(self) -> ApprovalMode:
        return self._mode_provider() if self._mode_provider is not None else ApprovalMode.INTERACTIVE

    async def run(self, input: ToolInput) -> ToolOutput:
        action = str(input.params.get("action", "")).strip()
        args = str(input.params.get("args", "")).strip()
        workspace = input.params.get("workspace") or None
        cwd = Path(workspace).resolve() if workspace else Path.cwd()

        if not shutil.which("git"):
            return ToolOutput(success=False, error="git is not installed or not in PATH.")

        try:
            cmd = _build_command(action, args)
        except ValueError as exc:
            return ToolOutput(success=False, error=f"Could not parse args: {exc}")
        if cmd is None:
            return ToolOutput(
                success=False,
                error=f"Unknown git action: {action!r}. "
                f"Valid: status, diff, log, branch, show, add, commit, push, pull, checkout, stash, merge.",
            )

        # Force pushes are hard-refused - lifted in autonomous mode, where the
        # operator has made the approval mode the only authority.
        mode = self._mode()
        if mode != ApprovalMode.AUTONOMOUS and action == "push" and any(_is_force_flag(t) for t in cmd[2:]):
            return ToolOutput(
                success=False,
                error="Force-push is blocked - too destructive. Push to a new branch instead.",
            )

        # An action that runs without an approval card has nobody checking its
        # arguments, so the allowlist is the only thing between the agent and
        # `git diff --output=/anywhere`.
        mutating = _is_mutating(action, cmd)
        if not mutating and (unsafe := _unsafe_flags(action, cmd[2:])):
            return ToolOutput(
                success=False,
                error=(
                    f"Option(s) not permitted for read-only `git {action}`: {' '.join(unsafe)}. "
                    "Read-only git runs without an approval card, so its options are limited to "
                    "an allowlist that cannot write files or run commands."
                ),
            )

        auto_git = mode in (ApprovalMode.AUTO, ApprovalMode.AUTONOMOUS) and self._unattended.approves_git(action, args)
        if mutating and not auto_git:
            denial = await gate_mutating_action(
                self._approval_store,
                agent="git",
                title="Git Operation - Approval Required",
                message=f"```\n{' '.join(cmd)}\n```",
                task_id=input.params.get("task_id"),
                stream_manager=self._stream_manager,
                judgement_filter=self._judgement_filter,
                notifier=self._notifier,
                timeout=self._approval_timeout_seconds,
            )
            if denial is not None:
                return denial

        return await asyncio.to_thread(run_capture, cmd, cwd, timeout=_TIMEOUT)


def _is_mutating(action: str, cmd: list[str]) -> bool:
    if action in _READONLY_ACTIONS:
        return False
    if action == "branch":
        # Listing branches is read-only. Any flag outside the list set
        # (create/delete/rename/move) makes it mutating. A positional is a
        # branch name to create - unless `--list` is present, which turns it
        # into a glob pattern to filter the listing by.
        tokens = cmd[2:]
        flags = [t for t in tokens if t.startswith("-")]
        if any(flag not in _BRANCH_LIST_FLAGS for flag in flags):
            return True
        positionals = [t for t in tokens if not t.startswith("-")]
        return bool(positionals) and not any(flag in _BRANCH_LIST_MODE_FLAGS for flag in flags)
    return True


def _build_command(action: str, args: str) -> list[str] | None:
    base: list[str] = ["git"]
    # shlex.split honours quoting so paths/refs containing spaces survive intact;
    # a malformed quote raises ValueError, surfaced as a tool error by the caller.
    arg_parts = shlex.split(args) if args else []

    match action:
        case "status":
            return base + ["status", "--short", "--branch"]
        case "diff":
            return base + ["diff"] + arg_parts
        case "log":
            return base + ["log", "--oneline", "--graph", "--decorate", "-20"] + arg_parts
        case "branch":
            return base + ["branch"] + arg_parts
        case "show":
            return base + ["show"] + (arg_parts or ["HEAD"])
        case "add":
            return base + ["add"] + (arg_parts or ["."])
        case "commit":
            if not args:
                return None  # message is required
            return base + ["commit", "-m", args]
        case "push":
            return base + ["push"] + arg_parts
        case "pull":
            return base + ["pull"] + arg_parts
        case "checkout":
            return base + ["checkout"] + arg_parts
        case "stash":
            return base + ["stash"] + arg_parts
        case "merge":
            return base + ["merge"] + arg_parts
        case _:
            return None
