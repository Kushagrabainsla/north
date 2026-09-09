"""Pydantic models and enums for the Approval Layer.

See docs/CODING_STYLE.md Section 7.3 and README Section 9.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class CardType(StrEnum):
    """The three supported card types in north."""

    INFORMATION = "information"
    APPROVAL = "approval"
    QUESTION = "question"


class ApprovalDecision(StrEnum):
    """Possible outcomes for a user approval card decision."""

    APPROVED = "approved"
    REJECTED = "rejected"
    ANSWERED = "answered"  # a QUESTION card was given a free-form/selected answer
    TIMEOUT_REJECTED = "timeout_rejected"
    # The task the card belonged to ended before the user answered (cancelled,
    # paused, failed, or killed as stuck). Not a decision - the question simply
    # stopped mattering, and the card must not keep asking.
    TASK_ENDED = "task_ended"


class CardFieldType(StrEnum):
    """How one field of a card is rendered and read back."""

    TEXT = "text"
    TEXTAREA = "textarea"
    NUMBER = "number"
    BOOLEAN = "boolean"
    SELECT = "select"
    LINK = "link"


class CardField(BaseModel):
    """One piece of the work a card is handing over.

    A card used to be a sentence and two buttons, which is enough to ask "may I
    do this?" and not enough to hand over something already filled in. A field
    is one line of that work: what it is called, what north put in it, and
    whether you may change it before saying yes.
    """

    name: str
    label: str = ""
    type: CardFieldType = CardFieldType.TEXT
    value: Any = ""
    editable: bool = False
    # Choices for `type == SELECT`; ignored otherwise.
    options: list[str] = Field(default_factory=list)

    def display_label(self) -> str:
        """The label to show, falling back to a readable form of the name."""
        return self.label or self.name.replace("_", " ").strip().capitalize()


class Card(BaseModel):
    """A card presented to the user via macOS notification or Web UI."""

    id: str
    type: CardType
    task_id: str
    agent: str
    title: str
    message: str
    options: list[str] = Field(default_factory=list)
    status: str = "pending"  # pending, approved, rejected, answered
    chosen_option: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Work north has filled in and is handing over for a decision. Empty for the
    # ordinary "may I run this command?" card, which is still just a message.
    fields: list[CardField] = Field(default_factory=list)
    # Read-only source material shown beside the fields - the job posting behind
    # a filled-in application. You cannot judge the work without what it came
    # from, and putting it in `message` would mix the evidence with the ask.
    context: str = ""
    # What came back: the field values as decided, after any edits. Written on
    # resolve, so a caller reads the *decided* work rather than what it proposed.
    response: dict[str, Any] = Field(default_factory=dict)
    # Whether something is waiting on the answer. True for a guard-rail - an
    # agent mid-action that cannot continue until you say yes. False for work
    # north has finished and left for you: the task ends, the card stays, and
    # you decide whenever. A blocking card cannot outlive its task; a
    # non-blocking one is meant to.
    blocking: bool = True
    # What produced this card, for a card that outlives the task that made it.
    # A card's life is otherwise scoped to its `task_id`, which is exactly wrong
    # for prepared work: the task finishing is the *normal* case there, not the
    # reason to throw the card away. Empty for an ordinary in-flight question.
    source: str = ""

    @property
    def outlives_task(self) -> bool:
        """Whether this card survives its task reaching a terminal state."""
        return bool(self.source) or not self.blocking

    def field_values(self) -> dict[str, Any]:
        """The values as north filled them in, before the user touched anything."""
        return {field.name: field.value for field in self.fields}

    def merge_response(self, values: dict[str, Any] | None) -> dict[str, Any]:
        """Overlay client-supplied *values* onto this card's own fields.

        The card is issued by the server, so the response is validated against
        it rather than trusted: unknown names are dropped and non-editable
        fields keep the value north put there. Without this, a client could
        rewrite the very part of the work the user was meant to be checking -
        the URL an application is submitted to, say - and the approval would
        still read as approving what was shown.
        """
        merged = self.field_values()
        if not values:
            return merged
        editable = {field.name: field for field in self.fields if field.editable}
        for name, raw in values.items():
            field = editable.get(name)
            if field is not None:
                merged[name] = _coerce(field, raw)
        return merged


def _coerce(field: CardField, raw: Any) -> Any:
    """Read *raw* as the field's declared type, keeping it on a bad value.

    Form inputs arrive as strings, so a number field comes back as "900". A
    value that will not convert is left as north had it: a malformed edit must
    not quietly become 0 or False in work the user believes they checked.
    """
    if field.type == CardFieldType.NUMBER:
        try:
            text = str(raw).strip()
            return int(text) if text.lstrip("-").isdigit() else float(text)
        except (TypeError, ValueError):
            return field.value
    if field.type == CardFieldType.BOOLEAN:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in ("true", "yes", "on", "1")
        return field.value
    if field.type == CardFieldType.SELECT:
        return raw if raw in field.options else field.value
    return raw
