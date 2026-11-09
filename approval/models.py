"""Pydantic models and enums for the Approval Layer.

See docs/CODING_STYLE.md Section 7.4 and README Section 9.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from config import secret_store
from utils.ids import generate_id
from utils.secrets import REDACTED, contains_secret, is_secret_field_name, redact

DEFAULT_APPROVE_OPTION = "Approve"
DEFAULT_REJECT_OPTION = "Reject"


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


class DecidedBy(StrEnum):
    """Who decided a card. Every surface shows it beside the decision (CODING_STYLE §7.3)."""

    YOU = "you"
    SAFE_LIST = "safe_list"
    REPLAY = "replay"
    MEMORY_DECIDER = "memory_decider"
    YOLO = "yolo"
    # North's own fixed rules: writing its own notes, refusing a catastrophic shape.
    NORTH = "north"

    @property
    def label(self) -> str:
        return _DECIDED_BY_LABELS[self]


_DECIDED_BY_LABELS: dict[DecidedBy, str] = {
    DecidedBy.YOU: "you",
    DecidedBy.SAFE_LIST: "the safe list",
    DecidedBy.REPLAY: "your past answer",
    DecidedBy.MEMORY_DECIDER: "the memory decider",
    DecidedBy.YOLO: "yolo",
    DecidedBy.NORTH: "north's fixed rules",
}


class MemoryKind(StrEnum):
    """What kind of memory a decision used."""

    FACT = "fact"
    EPISODE = "episode"
    PAST_DECISION = "past_decision"
    JUDGEMENT_RULES = "judgement_rules"
    PROFILE = "profile"

    @property
    def label(self) -> str:
        return _MEMORY_KIND_LABELS[self]


_MEMORY_KIND_LABELS: dict[MemoryKind, str] = {
    MemoryKind.FACT: "Fact",
    MemoryKind.EPISODE: "Past task",
    MemoryKind.PAST_DECISION: "Your past decision",
    MemoryKind.JUDGEMENT_RULES: "Your judgement rules",
    MemoryKind.PROFILE: "Your profile",
}


class MemoryRef(BaseModel):
    """One piece of memory a decision used, so the page can show it and link to it."""

    kind: MemoryKind
    text: str = ""
    # Where it lives, when it has an address: a past decision's fingerprint.
    ref: str = ""


class PriorDecision(BaseModel):
    """North's decision on a card, kept when you overrule it."""

    status: str
    chosen_option: str = ""
    decided_by: str = ""
    reason: str = ""
    memory_used: list[MemoryRef] = Field(default_factory=list)


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
    # For SELECT, the only choices. For an editable TEXT field, suggested answers
    # shown as buttons, which you may pick or type over (a question's options).
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
    # A real-world handoff, not evidence that the action has happened. Routing
    # still belongs to ApprovalPolicy; tools must verify state after an answer.
    requires_user_action: bool = False
    # What produced this card, for a card that outlives the task that made it.
    # A card's life is otherwise scoped to its `task_id`, which is exactly wrong
    # for prepared work: the task finishing is the *normal* case there, not the
    # reason to throw the card away. Empty for an ordinary in-flight question.
    source: str = ""
    # The identity of the action this card asks about (`Action.describe()`), so
    # the answer is learned under the same key the policy recalls it by. Set by
    # the server when the card is raised; empty for a card that asks about no
    # action, which is then never learned from.
    # Which action this card is about. `scrubbed` turns it into the action's fingerprint and
    # keeps a redacted `action_label`: the raw text can hold the very key the command sends.
    action_key: str = ""
    action_label: str = ""
    # Which of `options` mean no. Declared when the card is made: read from the
    # words, "Discard" and "Don't send" were recorded as approvals.
    rejecting: list[str] = Field(default_factory=list)
    # When it was decided; trimming history by creation time evicted a late answer at once.
    decided_at: datetime | None = None
    # Who decided it (a `DecidedBy`), why, and what memory led there. Empty while
    # the card waits. `reason` is also yours when you give one.
    decided_by: str = ""
    reason: str = ""
    memory_used: list[MemoryRef] = Field(default_factory=list)
    # North's decision, when you overruled it. The card keeps both.
    overruled: PriorDecision | None = None

    @model_validator(mode="after")
    def _reject_means_no(self) -> Card:
        """The plain Approve/Reject pair declares itself, for every way a card is made or read back."""
        if not self.rejecting and DEFAULT_REJECT_OPTION in self.options:
            self.rejecting = [DEFAULT_REJECT_OPTION]
        return self

    @field_validator("memory_used", mode="before")
    @classmethod
    def _read_plain_memory(cls, value: Any) -> Any:
        """Cards stored before memory was structured held plain "kind: text" strings."""
        if not isinstance(value, list):
            return value
        return [_memory_ref_from_text(item) if isinstance(item, str) else item for item in value]

    @classmethod
    def new(
        cls,
        *,
        type: CardType,
        agent: str,
        title: str,
        message: str,
        task_id: str | None = "",
        options: Sequence[str] = (),
        fields: Sequence[CardField] = (),
        context: str = "",
        blocking: bool = True,
        requires_user_action: bool = False,
        source: str = "",
        action_key: str = "",
        rejecting: Sequence[str] | None = None,
    ) -> Card:
        """Build a card with a fresh id and the defaults every caller wants.

        The one way a card comes into being. Six places used to assemble one by
        hand, each remembering to generate an id and coerce ``task_id`` - and
        each a place a new field could be forgotten. Mirrors ``LedgerEntry.new``.
        """
        return cls(
            id=generate_id(),
            type=type,
            task_id=task_id or "",
            agent=agent,
            title=title,
            message=message,
            options=list(options),
            rejecting=list(rejecting or ()),
            fields=list(fields),
            context=context,
            # Information is a one-way notification, not a decision. Keep that
            # invariant at construction so every notifier sees the truth even
            # when a caller does not go through UserInteraction.inform().
            blocking=False if type is CardType.INFORMATION else blocking,
            requires_user_action=requires_user_action,
            source=source,
            action_key=action_key,
        )

    @property
    def outlives_task(self) -> bool:
        """Whether this card survives its task reaching a terminal state."""
        return bool(self.source) or not self.blocking

    @property
    def asks_several(self) -> bool:
        """A question card whose fields are its questions, answered together on the one card."""
        return self.type is CardType.QUESTION and bool(self.fields)

    def answers_in(self, reply: str) -> dict[str, str]:
        """The answer *reply* gives to each of this card's questions, by field name; {} when it cannot tell.

        Telegram and the terminal take one message. A reply with one line per question, in
        order, numbered or not, answers each; so does one line with the answers split by ";".
        An option's number picks that option. Any other reply is not split up: the agent
        reads it whole.
        """
        if len(self.fields) == 1:
            return {self.fields[0].name: _picked(self.fields[0], reply.strip())} if reply.strip() else {}
        lines = [line for line in reply.splitlines() if line.strip()]
        if len(lines) == 1:
            lines = lines[0].split(";")
        if len(lines) != len(self.fields):
            return {}
        answers = zip(self.fields, lines, strict=True)
        return {field.name: _picked(field, _NUMBERED.sub("", line).strip()) for field, line in answers}

    def numbered_questions(self) -> list[str]:
        """Each question, numbered, with its options numbered under it: how a one-message channel shows them."""
        lines: list[str] = []
        for number, field in enumerate(self.fields, 1):
            lines.append(f"{number}. {field.display_label()}")
            lines.extend(f"    {index}) {option}" for index, option in enumerate(field.options, 1))
        return lines

    def answers_text(self, values: dict[str, Any]) -> str:
        """Each question with its answer from *values*, one per line; "" while any is unanswered.

        What a several-question card resolves to as its `chosen_option`, so everything that
        reads an answer (the ledger, past decisions, a task that starts over) reads them all.
        """
        answers = [(field.display_label(), str(values.get(field.name) or "").strip()) for field in self.fields]
        if not answers or not all(answer for _, answer in answers):
            return ""
        return "\n".join(f"{question} → {answer}" for question, answer in answers)

    def scrubbed(self) -> Card:
        """A copy with anything that looks like a credential taken out.

        A card can carry work north filled in, and those values are written to
        SQLite and audit-logged permanently - a filled-in form is exactly where
        an API key or a password ends up. `memory/facts.py` already refuses to
        *store* a fact containing one; a card cannot refuse, because it is the
        thing the user has to read before deciding, so it redacts instead.

        A field whose *name* says it holds a secret is redacted whatever its
        value: a password of "hunter2" matches no pattern, and the label is the
        evidence.
        """

        hide = secret_store.store().hide

        def clean(text: str) -> str:
            return hide(redact(text))

        def is_secret(name: str, value: Any) -> bool:
            if is_secret_field_name(name):
                return True
            return isinstance(value, str) and (contains_secret(value) or hide(value) != value)

        fields = [
            field.model_copy(update={"value": REDACTED}) if is_secret(field.name, field.value) else field
            for field in self.fields
        ]
        update: dict[str, Any] = {
            "fields": fields,
            "response": {name: REDACTED if is_secret(name, value) else value for name, value in self.response.items()},
            "title": clean(self.title),
            "message": clean(self.message),
            "context": clean(self.context),
        }
        if self.action_key and not _FINGERPRINT_RE.fullmatch(self.action_key):
            update |= {
                "action_key": action_fingerprint(self.agent, self.action_key),
                "action_label": clean(self.action_key),
            }

        # Most cards carry no secret, and every card passes through here on the
        # way into the queue. Returning self keeps that case free - and keeps a
        # card's identity, which callers holding the object they just added rely
        # on.
        if all(value == getattr(self, name) for name, value in update.items()):
            return self
        return self.model_copy(update=update)

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


# "1. " or "2) " in front of an answer in a one-message reply; "2026-11-05" and "1.5 hours" keep their digits.
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+")
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")


def action_fingerprint(agent: str, message: str) -> str:
    """Identity of an action: its whole text, exactly as given.

    *message* is `Action.describe()`. Nothing is normalized away: case and
    backticks change what an action does (`Report.txt` is another file,
    `` `touch f` `` runs a command), so both are part of the identity.

    This used to hash only the first 80 characters, which made any two actions
    sharing a long prefix the same action. A `cd`-and-activate preamble is
    routinely longer than that, so "run the tests" and "delete the data
    directory" could carry one fingerprint - and approving the first taught
    north to replay that approval for the second.

    Hashing everything makes a fingerprint narrower, not wider: a volatile tail
    (a diff hunk, a commit message) now makes an action look unique, so north
    asks again instead of replaying. That is the safe direction to be wrong in -
    an extra question costs a moment, a false match acts without being asked.

    A *message* that is already a fingerprint is returned as it is: a stored card holds
    only that, because the action's text can contain the very key a command sends.
    """
    if _FINGERPRINT_RE.fullmatch(message):
        return message
    return hashlib.sha256(f"{agent}::{message}".encode()).hexdigest()


def _picked(field: CardField, answer: str) -> str:
    """*answer* to a question, with an option's name in any case, or its number, read as that option."""
    named = next((option for option in field.options if option.lower() == answer.lower()), None)
    if named is not None:
        return named
    if answer.isdigit() and 1 <= int(answer) <= len(field.options):
        return field.options[int(answer) - 1]
    return answer


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


_LEGACY_MEMORY_PREFIXES: dict[str, MemoryKind] = {
    "fact": MemoryKind.FACT,
    "episode": MemoryKind.EPISODE,
    "past decision": MemoryKind.PAST_DECISION,
    "judgement rules": MemoryKind.JUDGEMENT_RULES,
    "your profile": MemoryKind.PROFILE,
}


def _memory_ref_from_text(text: str) -> MemoryRef:
    head, _, rest = text.partition(":")
    kind = _LEGACY_MEMORY_PREFIXES.get(head.strip(), MemoryKind.FACT)
    return MemoryRef(kind=kind, text=rest.strip() if head.strip() in _LEGACY_MEMORY_PREFIXES else text)
