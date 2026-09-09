"""Tests for cards that carry filled-in work rather than only a message.

Issue #11: an approval card was a sentence and two buttons, which can ask "may
I do this?" but cannot hand over something North has already filled in. These
cover the field/response half of that - what is shown, what may be edited, and
what comes back.
"""

from __future__ import annotations

import asyncio

import pytest

from approval.interaction import UserInteraction
from approval.models import ApprovalDecision, Card, CardField, CardFieldType, CardType
from approval.store import ApprovalStore


def _card(**overrides) -> Card:
    defaults = {
        "id": "card-1",
        "type": CardType.APPROVAL,
        "task_id": "task-1",
        "agent": "job",
        "title": "Application ready",
        "message": "Submit this application?",
    }
    return Card(**{**defaults, **overrides})


def _application_fields() -> list[CardField]:
    return [
        CardField(name="company", value="Acme", editable=False),
        CardField(name="url", type=CardFieldType.LINK, value="https://acme.test/jobs/1", editable=False),
        CardField(name="cover_letter", type=CardFieldType.TEXTAREA, value="Dear team,", editable=True),
        CardField(name="salary", type=CardFieldType.NUMBER, value=100, editable=True),
    ]


# ── The card model ───────────────────────────────────────────────────────────


def test_a_plain_card_is_unchanged() -> None:
    """The ordinary "may I run this?" card must not grow a form."""
    card = _card()

    assert card.fields == []
    assert card.context == ""
    assert card.response == {}
    assert card.field_values() == {}


def test_field_values_are_what_north_proposed() -> None:
    card = _card(fields=_application_fields())

    assert card.field_values() == {
        "company": "Acme",
        "url": "https://acme.test/jobs/1",
        "cover_letter": "Dear team,",
        "salary": 100,
    }


def test_an_edit_to_an_editable_field_is_kept() -> None:
    card = _card(fields=_application_fields())

    merged = card.merge_response({"cover_letter": "Dear Acme,"})

    assert merged["cover_letter"] == "Dear Acme,"


def test_a_read_only_field_cannot_be_rewritten_by_the_client() -> None:
    """The user approves what they were shown - not what the client sent back."""
    card = _card(fields=_application_fields())

    merged = card.merge_response({"url": "https://evil.test/collect", "company": "Other"})

    assert merged["url"] == "https://acme.test/jobs/1"
    assert merged["company"] == "Acme"


def test_unknown_field_names_are_dropped() -> None:
    card = _card(fields=_application_fields())

    merged = card.merge_response({"cover_letter": "ok", "injected": "surprise"})

    assert "injected" not in merged


def test_no_values_resolves_to_what_north_proposed() -> None:
    card = _card(fields=_application_fields())

    assert card.merge_response(None) == card.field_values()
    assert card.merge_response({}) == card.field_values()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("250", 250), (250, 250), ("12.5", 12.5), ("-3", -3)],
)
def test_a_number_field_reads_form_strings_as_numbers(raw: object, expected: object) -> None:
    card = _card(fields=[CardField(name="salary", type=CardFieldType.NUMBER, value=100, editable=True)])

    assert card.merge_response({"salary": raw})["salary"] == expected


def test_an_unreadable_number_keeps_what_north_had() -> None:
    """A malformed edit must not silently become 0 in work believed to be checked."""
    card = _card(fields=[CardField(name="salary", type=CardFieldType.NUMBER, value=100, editable=True)])

    assert card.merge_response({"salary": "abc"})["salary"] == 100


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(True, True), ("true", True), ("on", True), ("yes", True), ("false", False), ("", False)],
)
def test_a_boolean_field_reads_checkbox_values(raw: object, expected: bool) -> None:
    card = _card(fields=[CardField(name="remote", type=CardFieldType.BOOLEAN, value=False, editable=True)])

    assert card.merge_response({"remote": raw})["remote"] is expected


def test_a_select_field_rejects_a_value_outside_its_options() -> None:
    card = _card(
        fields=[
            CardField(name="tier", type=CardFieldType.SELECT, value="mid", editable=True, options=["mid", "senior"])
        ]
    )

    assert card.merge_response({"tier": "senior"})["tier"] == "senior"
    assert card.merge_response({"tier": "ceo"})["tier"] == "mid"


def test_display_label_falls_back_to_a_readable_name() -> None:
    assert CardField(name="cover_letter").display_label() == "Cover letter"
    assert CardField(name="cover_letter", label="Your letter").display_label() == "Your letter"


# ── Resolving through the store ──────────────────────────────────────────────


def test_resolving_stores_the_decided_values() -> None:
    store = ApprovalStore()
    store.add(_card(fields=_application_fields()))

    assert store.resolve("card-1", ApprovalDecision.APPROVED, values={"salary": "250"})

    resolved = store.get("card-1")
    assert resolved.status == ApprovalDecision.APPROVED
    assert resolved.response["salary"] == 250
    assert resolved.response["company"] == "Acme", "untouched fields are still part of what was decided"


def test_resolving_without_values_still_reports_the_proposed_work() -> None:
    """A learned rule or a timeout resolves a card nobody typed into."""
    store = ApprovalStore()
    store.add(_card(fields=_application_fields()))

    store.resolve("card-1", ApprovalDecision.TIMEOUT_REJECTED)

    assert store.get("card-1").response == _card(fields=_application_fields()).field_values()


def test_a_card_without_fields_gets_no_response() -> None:
    store = ApprovalStore()
    store.add(_card())

    store.resolve("card-1", ApprovalDecision.APPROVED, values={"anything": 1})

    assert store.get("card-1").response == {}


def test_an_already_resolved_card_cannot_be_rewritten() -> None:
    store = ApprovalStore()
    store.add(_card(fields=_application_fields()))
    store.resolve("card-1", ApprovalDecision.APPROVED, values={"salary": "250"})

    assert not store.resolve("card-1", ApprovalDecision.REJECTED, values={"salary": "1"})
    assert store.get("card-1").response["salary"] == 250


# ── Through UserInteraction ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_work_approval_returns_the_decided_values() -> None:
    store = ApprovalStore()
    interaction = UserInteraction(store)

    async def decide() -> None:
        card_id = next(iter(store.all(10))).id
        store.resolve(card_id, ApprovalDecision.APPROVED, values={"cover_letter": "Edited."})

    task = asyncio.create_task(
        interaction.request_work_approval(
            task_id="task-1",
            agent="job",
            title="Application ready",
            message="Submit?",
            fields=_application_fields(),
            context="the original posting",
            timeout=5,
        )
    )
    await asyncio.sleep(0)
    await decide()
    resolved = await task

    assert resolved.status == ApprovalDecision.APPROVED
    assert resolved.response["cover_letter"] == "Edited."
    assert resolved.context == "the original posting"


@pytest.mark.asyncio
async def test_an_unanswered_work_card_reports_what_was_proposed() -> None:
    store = ApprovalStore()
    interaction = UserInteraction(store)

    resolved = await interaction.request_work_approval(
        task_id="task-1",
        agent="job",
        title="Application ready",
        message="Submit?",
        fields=_application_fields(),
        timeout=0.01,
    )

    assert resolved.status == ApprovalDecision.TIMEOUT_REJECTED
    assert resolved.response["salary"] == 100, "a caller reading response must not get a bare {}"


@pytest.mark.asyncio
async def test_fields_and_context_reach_the_stream() -> None:
    class Recorder:
        def __init__(self) -> None:
            self.payloads: list[dict] = []

        async def emit(self, task_id: str, event: str, payload: dict) -> None:
            self.payloads.append(payload)

    stream = Recorder()
    store = ApprovalStore()
    interaction = UserInteraction(store, stream_manager=stream)

    await interaction.request_work_approval(
        task_id="task-1",
        agent="job",
        title="Application ready",
        message="Submit?",
        fields=_application_fields(),
        context="posting",
        timeout=0.01,
    )

    payload = stream.payloads[0]
    assert [f["name"] for f in payload["fields"]] == ["company", "url", "cover_letter", "salary"]
    assert payload["context"] == "posting"


@pytest.mark.asyncio
async def test_a_plain_approval_still_emits_empty_fields() -> None:
    """Clients read the key unconditionally, so it must always be present."""

    class Recorder:
        def __init__(self) -> None:
            self.payloads: list[dict] = []

        async def emit(self, task_id: str, event: str, payload: dict) -> None:
            self.payloads.append(payload)

    stream = Recorder()
    interaction = UserInteraction(ApprovalStore(), stream_manager=stream)

    await interaction.request_approval_status(
        task_id="task-1", agent="bash", title="Run?", message="rm -rf /tmp/x", timeout=0.01
    )

    assert stream.payloads[0]["fields"] == []
    assert stream.payloads[0]["context"] == ""
