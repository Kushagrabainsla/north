"""When a skill or flow step asks you before it acts.

Flows and skills share this vocabulary: a skill declares its baseline, a flow
step may require more. It lives here because neither module may import the
other.
"""

from __future__ import annotations

# When a step asks you, for a skill's execution baseline and for a flow step:
#   never        - the step may not change anything
#   on_mutation  - every change the step makes goes to the approval layer
#   before_step  - one card before the step starts, then its tools run freely
STEP_APPROVALS = ("never", "on_mutation", "before_step")
# `always` read as "always ask", but it meant "ask once, before the step" (#41).
# It still loads, as `before_step`.
_APPROVAL_ALIASES = {"always": "before_step"}


def parse_step_approval(raw: object) -> str:
    """A step's approval setting, with old names mapped to the current ones. Raises ValueError if unknown."""
    value = str(raw or "on_mutation").strip().lower()
    value = _APPROVAL_ALIASES.get(value, value)
    if value not in STEP_APPROVALS:
        raise ValueError(f"approval must be one of {', '.join(STEP_APPROVALS)} (not {raw!r})")
    return value


def approval_fingerprint_value(approval: str) -> str:
    """The value a fingerprint hashes, unchanged by the rename, so tested flows stay tested."""
    return "always" if approval == "before_step" else approval
