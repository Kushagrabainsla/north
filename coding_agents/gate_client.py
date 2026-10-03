"""Asking north's gate on behalf of an agent that has no hook: the one question Codex's approvals become."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import httpx

from coding_agents.gate import Decision
from coding_agents.models import GateAccess

logger = logging.getLogger(__name__)


async def ask_gate(gate: GateAccess, payload: Mapping[str, Any]) -> Decision:
    """The gate's answer to a hook-shaped *payload*, over the same route and token the hook uses.

    Anything but an explicit allow is a refusal: a pass means "the agent's own rules decide", and a Codex
    approval is the agent already saying its own rules need an answer. An unreachable gate refuses too.
    There is no timeout: an approval waits for a person.
    """
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            response = await client.post(gate.url, json=dict(payload), headers={"X-Gate-Token": gate.token})
        response.raise_for_status()
        decision = response.json()["hookSpecificOutput"]["permissionDecision"]
        return Decision(decision) if decision in (Decision.ALLOW, Decision.DENY) else Decision.DENY
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        logger.warning("north's gate could not be asked (%s); refusing", exc)
        return Decision.DENY
