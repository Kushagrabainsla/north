"""Interpretation of serialized tool results produced during an agent run.

Every reader here is deliberately fail-safe: unparseable output is treated as a
plain failure rather than raising inside the agent loop.
"""

from __future__ import annotations

import json


def extract_success(tool_result: str) -> bool:
    """Return whether a serialized tool result reports success."""
    try:
        return bool(json.loads(tool_result).get("success", False))
    except (json.JSONDecodeError, AttributeError):
        return False


def is_unanswered_approval(tool_result: str) -> bool:
    """Return whether an approval card expired with nobody answering it.

    Gated tools carry the marker under ``data``; the loop's own approval
    built-in reports it at the top level.
    """
    try:
        parsed = json.loads(tool_result)
    except (json.JSONDecodeError, AttributeError):
        return False
    if not isinstance(parsed, dict):
        return False
    if parsed.get("unanswered"):
        return True
    data = parsed.get("data")
    return bool(isinstance(data, dict) and data.get("unanswered"))


def failure_kind(tool_result: str) -> str:
    """Return ``error``, ``not_found``, or ``refused`` for a failed tool call."""
    try:
        return str(json.loads(tool_result).get("failure_kind") or "error")
    except (json.JSONDecodeError, AttributeError):
        return "error"


def is_delegation_failure(tool_result: str) -> bool:
    """Return whether delegation genuinely failed, ignoring control-flow guards."""
    try:
        return bool(json.loads(tool_result).get("delegation_failed", False))
    except (json.JSONDecodeError, AttributeError):
        return False


def failed_json(message: str) -> str:
    """Serialize a failed tool result carrying *message*."""
    return json.dumps({"success": False, "error": message})
