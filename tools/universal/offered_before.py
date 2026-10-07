"""Whether north has already put an item in front of the user, so a daily flow does not offer it again."""

from __future__ import annotations

import asyncio

from tools.base import Tool
from tools.models import ToolInput, ToolOutput

# Everything left for the user shares one memory: the same job is a repeat whichever flow found it.
OFFERED_SCOPE = "left_for_user"


class OfferedBeforeTool(Tool):
    name = "offered_before"
    description = (
        "Check whether north has already offered the user an item - a job, a candidate, an article - "
        "before doing any work on it. Pass every identity it has: its address and a 'company | role' "
        "style name. True means skip it: it was offered before, and offering it again with "
        "request_approval(wait=false, item_keys=...) would make no card anyway."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "item_keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The item's identities, e.g. its address and 'Acme | Software Engineer'.",
            }
        },
        "required": ["item_keys"],
    }

    def __init__(self, offered_store) -> None:
        self._offered = offered_store

    async def run(self, input: ToolInput) -> ToolOutput:
        keys = [str(key) for key in input.params.get("item_keys") or [] if str(key).strip()]
        if not keys:
            return ToolOutput(success=False, error="Give at least one item key.")
        return ToolOutput(
            success=True, data={"offered": await asyncio.to_thread(self._offered.seen, OFFERED_SCOPE, *keys)}
        )
