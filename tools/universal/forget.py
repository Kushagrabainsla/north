"""forget - remove facts north remembers about the user, when they ask.

Asked "pls forget my favourite food, i dont want you keeping that", north said it
had "no tool that edits or removes stored personal facts" and that someone with
access to its store would have to delete it. Only the Memory page could. This
tool removes what the user names, with its earlier values, and always asks
first: the card lists exactly the facts that go, and nothing else is removed.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from memory.profile import rebuild_profile
from tools import Tool, ToolInput, ToolOutput
from tools.base import prepared

if TYPE_CHECKING:
    from approval.approvals import Request
    from memory.base import ContextStore
    from memory.facts import FactStore

_SHOWN_ON_CARD = 20
_STAYS = "Your past messages stay in the task history; this removes what north remembers and recalls."


class ForgetTool(Tool):
    """Forget the facts the user names, after they confirm the exact list."""

    name = "forget"
    description = (
        "Forget facts north remembers about the user, only when they ask you to forget something or stop "
        "remembering it ('forget my favourite food'). Not for a correction or an update: north replaces the old "
        "fact by itself. Removes every remembered fact whose text contains the words in `what`, with the earlier "
        "values of the same thing. The user always confirms the exact list first. "
        "Name the thing in the user's own words, not a whole sentence."
    )
    is_mutating = True
    parameters_schema = {
        "type": "object",
        "properties": {
            "what": {
                "type": "string",
                "description": "What to forget, in the user's words: 'favourite food', 'penicillin allergy'.",
            }
        },
        "required": ["what"],
    }

    def __init__(self, fact_store: FactStore, context_store: ContextStore) -> None:
        self._facts = fact_store
        self._documents = context_store

    async def describe(self, input: ToolInput) -> Request | None:
        """The exact facts that would go. Never replayed, and asked in every mode: forgetting is not undone."""
        from approval.approvals import Request
        from approval.policy import Action, ActionKind

        what = str(input.params.get("what") or "").strip()
        facts = await self._facts.matching(what)
        if not facts:
            return None
        ids = sorted(fact["id"] for fact in facts)
        lines = [
            f"- {fact['content']}" + ("" if fact["status"] == "active" else " (an earlier value)")
            for fact in facts[:_SHOWN_ON_CARD]
        ]
        if len(facts) > _SHOWN_ON_CARD:
            lines.append(f"- and {len(facts) - _SHOWN_ON_CARD} more")
        return Request(
            action=Action(
                agent=self.name,
                kind=ActionKind.OTHER,
                summary=f"forget {len(facts)} fact(s) matching {what!r}",
                operation="forget",
                args=",".join(ids),
                content=hashlib.sha256("\n".join(f["content"] for f in facts).encode("utf-8")).hexdigest(),
                changes_north=True,
            ),
            title="Forget - Approval Required",
            message="North will forget:\n" + "\n".join(lines) + f"\n\n{_STAYS}",
            prepared=ids,
            requires_confirmation=True,
        )

    async def run(self, input: ToolInput) -> ToolOutput:
        what = str(input.params.get("what") or "").strip()
        ids: list[str] | None = prepared(input)
        if not ids:
            return ToolOutput(
                success=False,
                failure_kind="not_found",
                error=f"Nothing north remembers matches {what!r}, so nothing was forgotten. "
                "Try the words the fact itself would use.",
            )
        forgotten = await self._facts.forget(ids)
        # The profile is written from the facts; left alone it would still say what was forgotten.
        await rebuild_profile(self._facts, self._documents)
        return ToolOutput(success=True, data={"forgotten": forgotten, "note": _STAYS})

    def format_output(self, data: dict[str, Any]) -> str:
        forgotten = data.get("forgotten") or []
        listed = "\n".join(f"- {fact}" for fact in forgotten)
        return f"Forgot {len(forgotten)} fact(s):\n{listed}\n\n{data.get('note', '')}".strip()
