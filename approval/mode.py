"""Approval mode - one dial for how much north does without asking.

Three ordered tiers, from most to least supervised:

- ``interactive`` (default): read-only actions run freely, but every *mutating*
  action asks for approval. Full human-in-the-loop.
- ``auto``: additionally auto-approves a fixed, deterministic SAFE subset needed
  for engineering work - editing files inside the task workspace, running an
  allowlist of test/lint/build commands, and local git (add/commit/branch). Any
  other mutating action still asks.
- ``autonomous``: auto-approves everything. There is no hard-danger floor - the
  patterns other modes refuse outright (``rm -rf /``, force-push) are allowed
  here, because choosing this mode is choosing to make the mode the only
  authority. Fully hands-off, and meant that way.

This replaces the older ``unattended_mode`` / ``autonomous_mode`` boolean pair; both
are still honoured as a fallback so existing configs keep working.

Whether an action may run without asking is decided in exactly one place -
``approval/policy.py``. Nothing else may branch on the mode to answer *that*
question: seven scattered copies of it is what this replaced, and one of them
had forgotten to check the mode at all.

A different question - "is there a human here to ask?" - is read from the mode
in two other places (the agent loop before asking a clarifying question, and the
Orchestrator's ``_human_available``). Those decide whether to *interrupt*, not
whether something is permitted, so they are not copies of the above.
"""

from __future__ import annotations

from enum import StrEnum


class ApprovalMode(StrEnum):
    INTERACTIVE = "interactive"  # read-only auto; every mutation asks (default)
    AUTO = "auto"  # + auto-approve the safe engineering subset
    AUTONOMOUS = "autonomous"  # auto-approve everything, no exceptions


# Friendly synonyms so a user can write what they mean.
_ALIASES: dict[str, ApprovalMode] = {
    "readonly": ApprovalMode.INTERACTIVE,
    "read-only": ApprovalMode.INTERACTIVE,
    "read_only": ApprovalMode.INTERACTIVE,
    "ask": ApprovalMode.INTERACTIVE,
    "manual": ApprovalMode.INTERACTIVE,
    "default": ApprovalMode.INTERACTIVE,
    "unattended": ApprovalMode.AUTO,
    "assisted": ApprovalMode.AUTO,
    "safe": ApprovalMode.AUTO,
    "full": ApprovalMode.AUTONOMOUS,
    "yolo": ApprovalMode.AUTONOMOUS,
    "all": ApprovalMode.AUTONOMOUS,
}


def parse_approval_mode(raw: str | None) -> ApprovalMode | None:
    """Parse a mode name (or synonym), or None if *raw* is empty/unrecognised."""
    key = (raw or "").strip().lower()
    if not key:
        return None
    if key in _ALIASES:
        return _ALIASES[key]
    try:
        return ApprovalMode(key)
    except ValueError:
        return None


def approve_option(options: list[str]) -> str:
    """Pick the option string that means 'approve' for a card."""
    for opt in options or []:
        if opt.lower() in ("approve", "apply", "run", "yes", "allow", "proceed"):
            return opt
    return (options or ["Approve"])[0]


def resolve_approval_mode(settings: object) -> ApprovalMode:
    """Determine the effective approval mode from settings.

    ``autonomy`` (new name) wins when set; ``approval_mode`` is honoured as a
    legacy fallback; otherwise the legacy ``autonomous_mode`` / ``unattended_mode``
    booleans are honoured; otherwise the default (interactive).
    """
    explicit = parse_approval_mode(getattr(settings, "autonomy", None))
    if explicit is None:
        explicit = parse_approval_mode(getattr(settings, "approval_mode", None))
    if explicit is not None:
        return explicit
    if bool(getattr(settings, "autonomous_mode", False)):
        return ApprovalMode.AUTONOMOUS
    if bool(getattr(settings, "unattended_mode", False)):
        return ApprovalMode.AUTO
    return ApprovalMode.INTERACTIVE
