"""Approval mode - one dial for how much north does without asking.

Four modes, from most to least supervised (CODING_STYLE §7.3):

- ``ask`` (default): read-only runs; every mutating action asks.
- ``safe``: read-only, safe mutating actions and exact replays of your past
  answers run; everything else asks. No model decides anything.
- ``autonomous``: never asks.
- ``yolo``: never asks; every approval is yes and every question gets the yes
  answer.

Older names still load as aliases (``interactive``, ``auto``, the
``unattended_mode``/``autonomous_mode`` booleans), so no stored setting breaks.

Whether an action may run without asking is decided in exactly one place -
``approval/policy.py``. This module only names the modes, so every surface (the
settings API, dashboard, TUI, CLI, Telegram, ``north_config``) shows and accepts
the same list from one definition instead of keeping its own copy.

It lives with configuration rather than with approval because the mode is a
value the user sets and everything else reads: ``config/strategy.py`` loads and
persists it without configuration depending upward on the approval machinery.
"""

from __future__ import annotations

from enum import StrEnum


class ApprovalMode(StrEnum):
    ASK = "ask"
    SAFE = "safe"
    AUTONOMOUS = "autonomous"
    YOLO = "yolo"

    @property
    def description(self) -> str:
        return _DESCRIPTIONS[self]


_DESCRIPTIONS: dict[ApprovalMode, str] = {
    ApprovalMode.ASK: "Read-only work runs; every change asks you.",
    ApprovalMode.SAFE: "Read-only work, safe changes and your exact past answers run; everything else asks.",
    ApprovalMode.AUTONOMOUS: "Never asks you.",
    ApprovalMode.YOLO: "Never asks you: every approval is yes and every question gets the yes answer.",
}

# What YOLO answers to a question that offers no options.
YES_ANSWER = "Yes. Proceed with your best judgement."

# Older and friendlier names, so a stored setting or a typed word still means something.
_ALIASES: dict[str, ApprovalMode] = {
    "interactive": ApprovalMode.ASK,
    "manual": ApprovalMode.ASK,
    "default": ApprovalMode.ASK,
    "readonly": ApprovalMode.ASK,
    "read-only": ApprovalMode.ASK,
    "read_only": ApprovalMode.ASK,
    "auto": ApprovalMode.SAFE,
    "unattended": ApprovalMode.SAFE,
    "assisted": ApprovalMode.SAFE,
    "full": ApprovalMode.YOLO,
    "all": ApprovalMode.YOLO,
}


def parse_approval_mode(raw: str | None) -> ApprovalMode | None:
    """Parse a mode name (or alias), or None if *raw* is empty/unrecognised."""
    key = (raw or "").strip().lower()
    if not key:
        return None
    if key in _ALIASES:
        return _ALIASES[key]
    try:
        return ApprovalMode(key)
    except ValueError:
        return None


def require_approval_mode(raw: str | None) -> ApprovalMode:
    """Parse a mode the user asked for, or raise saying which modes exist. Every setter uses this."""
    mode = parse_approval_mode(raw)
    if mode is None:
        raise ValueError(f"Unknown approval mode {raw!r}. Valid: {', '.join(ApprovalMode)}.")
    return mode


def mode_options() -> list[dict[str, str]]:
    """The modes as every surface shows them, in order from most to least supervised."""
    return [{"value": mode.value, "description": mode.description} for mode in ApprovalMode]


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
    booleans are honoured; otherwise the default (ask).
    """
    explicit = parse_approval_mode(getattr(settings, "autonomy", None))
    if explicit is None:
        explicit = parse_approval_mode(getattr(settings, "approval_mode", None))
    if explicit is not None:
        return explicit
    if bool(getattr(settings, "autonomous_mode", False)):
        return ApprovalMode.AUTONOMOUS
    if bool(getattr(settings, "unattended_mode", False)):
        return ApprovalMode.SAFE
    return ApprovalMode.ASK
