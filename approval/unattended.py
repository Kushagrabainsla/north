"""Deterministic unattended-approval policy for autonomous / headless runs.

north gates every mutating action (file patches, shell commands, git writes)
behind a human approval card - correct for interactive use, but in a headless /
API run with no one to approve, the coder's edits and the reviewer's test runs
are denied and the task silently does nothing.

This policy lets the operator opt into *deterministic* auto-approval of a tightly
scoped set of SAFE actions - editing files inside the task's own workspace, and
running a known allowlist of test/lint/build commands - without an LLM in the
loop (unlike the JudgementFilter, which is non-deterministic and is forbidden
from auto-approving these agents anyway). Everything outside the safe set still
requires a human, so enabling this never opens the gate on destructive actions.

Off by default. Enable with ``unattended_mode`` in settings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from tools._path import is_sensitive_path

# Command programs that are safe to run unattended: test runners, type checkers,
# linters, formatters, and build/verify steps. Matched as a whole first token or
# a two-token prefix; a bare interpreter (e.g. `python -c ...`) is NOT included.
_DEFAULT_ALLOWED_COMMANDS: tuple[str, ...] = (
    "pytest",
    "python -m pytest",
    "python3 -m pytest",
    "go test",
    "npm test",
    "npm run test",
    "yarn test",
    "pnpm test",
    "cargo test",
    "ruff check",
    "ruff format",
    "mypy",
    "tsc",
    "npx tsc",
    "go vet",
    "go build",
    "make test",
)

# Any of these makes a command unsafe to auto-run: command chaining, command
# substitution, subshells, pipes, or writing redirects. A benign trailing
# ``2>&1`` (merge stderr into stdout) is stripped before this check.
_DANGEROUS = re.compile(r"[;&|`$<>(){}\n]")
_TRAILING_STDERR_REDIRECT = re.compile(r"\s+2>&1\s*$")

# Git actions safe to auto-run unattended: they are local, reversible, and stay
# within the workspace. Network (push/pull) and merge are never auto-approved.
_SAFE_GIT_ACTIONS: frozenset[str] = frozenset(
    {"status", "diff", "log", "show", "branch", "checkout", "add", "commit", "stash"}
)
# Even for a safe action, these argument flags are destructive - never auto-run.
_GIT_DANGEROUS_ARGS: frozenset[str] = frozenset({"-f", "--force", "-D", "--delete", "--hard"})


@dataclass(frozen=True)
class UnattendedPolicy:
    """Deterministic predicates for the SAFE action subset (the `auto` tier).

    Pure logic: whether a *given* action is in the safe subset. Whether that
    subset is auto-approved is decided by the caller from the live approval mode,
    so a mode change takes effect immediately without rebuilding this policy.
    """

    allowed_commands: tuple[str, ...] = field(default_factory=lambda: _DEFAULT_ALLOWED_COMMANDS)

    def approves_edit(self, resolved_path: Path, workspace: str | None) -> bool:
        """True when an edit to *resolved_path* is inside *workspace* and not sensitive."""
        if not workspace:
            return False
        if is_sensitive_path(resolved_path):
            return False
        try:
            resolved_path.resolve().relative_to(Path(workspace).resolve())
        except (ValueError, OSError):
            return False
        return True

    def approves_command(self, command: str) -> bool:
        """True when *command* matches the safe allowlist with no chaining."""
        cleaned = _TRAILING_STDERR_REDIRECT.sub("", command.strip())
        if not cleaned or _DANGEROUS.search(cleaned):
            return False
        return any(cleaned == p or cleaned.startswith(p + " ") for p in self.allowed_commands)

    def approves_git(self, action: str, args: str = "") -> bool:
        """True for a safe, local git action (never push/pull/merge/force)."""
        if action.strip() not in _SAFE_GIT_ACTIONS:
            return False
        return not any(tok in _GIT_DANGEROUS_ARGS for tok in args.split())

    @classmethod
    def from_settings(cls, settings: object) -> UnattendedPolicy:
        extra = tuple(getattr(settings, "unattended_extra_commands", ()) or ())
        return cls(allowed_commands=_DEFAULT_ALLOWED_COMMANDS + extra)
