"""Approval mode - one dial for how much north does without asking.

Four modes, from most to least supervised (CODING_STYLE §7.3):

- ``ask`` (default): read-only runs; every mutating action asks.
- ``safe``: read-only, safe mutating actions and exact replays of your past
  answers run; everything else asks. No model decides anything.
- ``autonomous``: decides for you from what north knows about you; asks only
  when that cannot decide, as for a step outside the workspace that nothing you
  said covers.
- ``yolo``: never asks; every approval is yes and every question gets the yes
  answer (or, when no option says yes, "proceed with your best judgement").

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
    ApprovalMode.AUTONOMOUS: (
        "Decides for you from what north knows about you. Asks only when that cannot decide: "
        "a step outside the workspace that nothing you said covers."
    ),
    ApprovalMode.YOLO: "Never asks you: every approval is yes and every question gets the yes answer.",
}

# What YOLO answers to a question that offers no options, or none that says yes.
YES_ANSWER = "Yes. Proceed with your best judgement."


def parse_approval_mode(raw: str | None) -> ApprovalMode | None:
    """Parse a mode name, or None if *raw* is empty/unrecognised."""
    key = (raw or "").strip().lower()
    if not key:
        return None
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


_YES_WORDS = frozenset({"approve", "apply", "run", "yes", "allow", "proceed"})


def affirmative_option(options: list[str]) -> str | None:
    """The option that says yes ("Approve", "Yes, delete it"), or None when none does."""
    for opt in options or []:
        words = opt.lower().replace(",", " ").split()
        if words and words[0].strip(".!:") in _YES_WORDS:
            return opt
    return None


def approve_option(options: list[str]) -> str:
    """Pick the option string that means 'approve' for a card."""
    return affirmative_option(options) or (options or ["Approve"])[0]


def resolve_approval_mode(settings: object) -> ApprovalMode:
    """The mode NORTH_APPROVAL_MODE sets, or ask when it is empty or not a mode."""
    return parse_approval_mode(getattr(settings, "approval_mode", None)) or ApprovalMode.ASK
