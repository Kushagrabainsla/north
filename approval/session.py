"""The run an approval belongs to: where it came from, and what the user said in it.

The memory decider read memory and the agent's text, never the user's request. Asked
in chat for a camera recording, it refused because "nothing the user stated
authorizes recording video", and it refused again after the user said yes in the
next message (issue #82, item 1).

A chat run is a task that a session turn started or joined (web, CLI, TUI). Its
messages are the user's own words, so the decider reads them as trusted. North's
answers are not included: they are agent text, which an approval never trusts.
Any other run (a schedule, a flow, a webhook) has no session, and nobody watches it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Session:
    """Whether the user started this run from chat, and their messages in that chat, oldest first."""

    from_chat: bool = False
    messages: tuple[str, ...] = ()


NO_SESSION = Session()

# What the policy calls to find the session of a task id.
SessionReader = Callable[[str], Awaitable[Session]]
