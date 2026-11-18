"""remember - keep a fact about the user that north found, or that the user asked it to keep.

Asked to store facts from a scan of local files, north saved 3 facts, all from the
user's own sentences, and nothing from the files: no tool let a run write a fact,
only the extractor that reads chat messages. Three days later north still stated a
fact the scan had shown to be out of date (issue #82, item 9).

A fact is filed under what it is about, so a newer one replaces the older, which is
kept as an earlier value, as for what the user says in chat. A fact north found by
itself is kept as unconfirmed, with where it was read, until the user states it:
recall shows "unconfirmed: north read it in <source>". The pair of `forget`.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from memory.profile import rebuild_profile
from tools import Tool, ToolInput, ToolOutput

if TYPE_CHECKING:
    from approval.approvals import Request
    from memory.base import ContextStore
    from memory.facts import FactStore

# The category facts from the user's own messages are filed under, so a found fact and a
# stated one about the same thing replace each other.
_CATEGORY = "user"
_QUOTE_CHARS = 500


class RememberTool(Tool):
    """Remember one fact about the user, with where it came from."""

    name = "remember"
    description = (
        "Remember one fact about the user so north can recall it in later tasks: something you found in a file, "
        "a page or a tool result, or something the user asked you to remember. One short sentence per call, about "
        "the user ('The user works at Acme as a data engineer.'). Give `about`, a short label for what the fact is "
        "about ('current job'): a newer fact about the same label replaces the older one. A fact you found is kept "
        "as unconfirmed, with `source`, until the user states it; set `said_by_user` only when the user stated "
        "the fact in this conversation. Never store secrets. To remove a fact, use `forget`."
    )
    is_mutating = True
    parameters_schema = {
        "type": "object",
        "properties": {
            "fact": {"type": "string", "description": "The fact, one sentence about the user."},
            "about": {
                "type": "string",
                "description": "What it is about, in a few words: 'current job', 'home city', 'resume'.",
            },
            "source": {
                "type": "string",
                "description": "Where you found it: a file path or a URL. Required unless said_by_user.",
            },
            "quote": {"type": "string", "description": "The words in the source that say it (optional)."},
            "said_by_user": {
                "type": "boolean",
                "description": "True only when the user stated this fact in this conversation. Default false.",
            },
        },
        "required": ["fact", "about"],
    }

    def __init__(self, fact_store: FactStore, context_store: ContextStore) -> None:
        self._facts = fact_store
        self._documents = context_store

    async def describe(self, input: ToolInput) -> Request | None:
        from approval.approvals import Request
        from approval.policy import Action, ActionKind

        fact = str(input.params.get("fact") or "").strip()
        if not fact:
            return None
        about = str(input.params.get("about") or "").strip()
        source = str(input.params.get("source") or "").strip()
        said = bool(input.params.get("said_by_user"))
        where = "you said it" if said else f"found in {source or 'an unnamed source'}, kept as unconfirmed"
        return Request(
            action=Action(
                agent=self.name,
                kind=ActionKind.OTHER,
                summary=f"remember: {fact}",
                operation="remember",
                args=about,
                details=f"{fact}\nabout: {about}\n{where}",
                content=hashlib.sha256(f"{fact}\n{about}\n{source}\n{said}".encode()).hexdigest(),
            ),
            title="Remember - Approval Required",
            message=f"North will remember:\n- {fact}\n\nAbout: {about or '-'} ({where}).",
        )

    async def run(self, input: ToolInput) -> ToolOutput:
        fact = str(input.params.get("fact") or "").strip()
        about = str(input.params.get("about") or "").strip()
        source = str(input.params.get("source") or "").strip()
        quote = str(input.params.get("quote") or "").strip()[:_QUOTE_CHARS]
        said = bool(input.params.get("said_by_user"))
        if not fact or not about:
            return ToolOutput(success=False, error="Give the fact and what it is about (`fact`, `about`).")
        if not said and not source:
            return ToolOutput(
                success=False,
                error="Say where you found it (`source`: a file path or a URL), or set `said_by_user` "
                "when the user stated it in this conversation.",
            )
        stored = await self._facts.add_fact_with_provenance(
            content=fact,
            category=_CATEGORY,
            about=about,
            source_path=source or None,
            evidence=quote or None,
            confirmed=said,
        )
        # The profile is written from the facts; left alone it would not show this one.
        await rebuild_profile(self._facts, self._documents)
        return ToolOutput(
            success=True,
            data={"fact": fact, "about": about, "new": stored, "confirmed": said, "source": source},
        )

    def format_output(self, data: dict[str, Any]) -> str:
        state = "" if data.get("confirmed") else f" (unconfirmed; found in {data.get('source') or 'a source'})"
        verb = "Remembered" if data.get("new") else "Already knew, and refreshed"
        return f"{verb}: {data.get('fact')}{state}"
