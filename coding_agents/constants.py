"""Tuning constants for delegated coding runs."""

from __future__ import annotations

# A plan run reads and thinks; this caps how long it may do either.
DEFAULT_MAX_TURNS = 40
DEFAULT_MAX_BUDGET_USD = 2.0
# A hard stop for a run that neither finishes nor errors. Approvals are not counted here: they
# never expire (CODING_STYLE 13.5) and a plan run asks for none.
RUN_TIMEOUT_SECONDS = 1800.0
# How long a signalled agent gets to exit before it is killed.
TERMINATE_GRACE_SECONDS = 5.0
PROBE_TIMEOUT_SECONDS = 15.0
# How long Claude Code waits for the gate to answer. An approval waits for a person and never expires
# (CODING_STYLE 13.5), so this is a day, not a limit anyone meets.
HOOK_TIMEOUT_SECONDS = 86_400
# One stream-json line can hold a whole file the agent read.
STREAM_LINE_LIMIT_BYTES = 32 * 1024 * 1024
# What the agent said last, kept when a run ends without a final answer.
MAX_TEXT_CHARS = 30_000
MAX_STDERR_CHARS = 2_000
MAX_EVENT_TEXT_CHARS = 500

# Paths a worker never reads, under the vendor sandbox's `denyRead`. North's own home is added
# by the caller, since it can be moved.
SECRET_PATHS: tuple[str, ...] = ("~/.north", "~/.ssh", "~/.aws", "~/.gnupg", "~/.codex", "~/.config/gh")

# The environment variables a worker inherits. North's own secrets and every other provider's
# API key stay out: the agent signs in through its own CLI login.
INHERITED_ENVIRONMENT: tuple[str, ...] = (
    "HOME",
    "PATH",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "TERM",
    "TMPDIR",
    "TZ",
    "__CF_USER_TEXT_ENCODING",
)
