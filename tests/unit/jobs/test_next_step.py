"""The job a decided card produces, and the retry rule that governs it.

The rule worth stating plainly: a step that sends something on your behalf is
never retried automatically. Those half-fail constantly - session expired,
captcha, submit clicked but the response lost - and a retry then applies twice.
Applying twice is worse than not applying.
"""

from __future__ import annotations

import pytest

from approval.continuation import CardContinuations
from approval.models import ApprovalDecision, Card, CardField, CardType
from approval.store import ApprovalStore
from jobs.models import Job, JobStatus
from jobs.next_step import enqueue_next_step, render


class _Processor:
    def __init__(self) -> None:
        self.jobs: list[Job] = []

    async def enqueue(self, job: Job) -> str:
        self.jobs.append(job)
        return job.job_id


def _card(**kw) -> Card:
    defaults = {
        "type": CardType.APPROVAL,
        "agent": "general",
        "title": "Application ready",
        "message": "Send it?",
        "source": "job_applications",
        "blocking": False,
        "fields": [CardField(name="company", value="Acme"), CardField(name="cover", value="Dear…", editable=True)],
    }
    defaults.update(kw)
    return Card.new(**defaults)


@pytest.fixture
def wiring():
    continuations = CardContinuations()
    return continuations, ApprovalStore(continuations=continuations), _Processor()


@pytest.mark.asyncio
async def test_a_decision_enqueues_a_job_built_from_your_values(wiring) -> None:
    continuations, store, processor = wiring
    continuations.register(
        "job_applications",
        enqueue_next_step(processor, agent="general", prompt_template="Apply to {company} with: {cover}"),
    )
    card = _card()
    store.add(card)

    store.resolve(card.id, ApprovalDecision.APPROVED, values={"cover": "My own words"})
    await continuations.drain()

    assert len(processor.jobs) == 1
    assert processor.jobs[0].task == "Apply to Acme with: My own words"
    assert processor.jobs[0].payload["card_id"] == card.id


@pytest.mark.asyncio
async def test_a_step_that_sends_is_never_retried(wiring) -> None:
    continuations, store, processor = wiring
    continuations.register(
        "job_applications",
        enqueue_next_step(processor, agent="general", prompt_template="Submit to {company}", sends_something=True),
    )
    card = _card()
    store.add(card)

    store.resolve(card.id, ApprovalDecision.APPROVED)
    await continuations.drain()

    assert processor.jobs[0].max_retries == 0


@pytest.mark.asyncio
async def test_an_ordinary_step_keeps_its_retries(wiring) -> None:
    """Not every next step submits something; only those get the zero."""
    continuations, store, processor = wiring
    continuations.register(
        "job_applications",
        enqueue_next_step(processor, agent="general", prompt_template="Draft a summary of {company}"),
    )
    card = _card()
    store.add(card)

    store.resolve(card.id, ApprovalDecision.APPROVED)
    await continuations.drain()

    assert processor.jobs[0].max_retries == 3


@pytest.mark.asyncio
async def test_a_step_registered_for_approve_does_not_fire_on_reject(wiring) -> None:
    continuations, store, processor = wiring
    continuations.register(
        "job_applications",
        enqueue_next_step(processor, agent="general", prompt_template="Apply to {company}"),
    )
    card = _card()
    store.add(card)

    store.resolve(card.id, ApprovalDecision.REJECTED)
    await continuations.drain()

    assert processor.jobs == []


def test_a_template_naming_an_unknown_field_does_not_raise() -> None:
    """Failing here would break the follow-up to a decision the user already made."""
    assert "(salary not provided)" in render("Apply to {company} for {salary}", {"company": "Acme"})


@pytest.mark.asyncio
async def test_a_job_that_must_not_retry_lands_in_needs_attention(tmp_path) -> None:
    """The other half of max_retries=0: it surfaces rather than being buried."""
    from jobs.sqlite_processor import SQLiteJobProcessor
    from utils.ids import generate_id
    from utils.time import utcnow

    processor = SQLiteJobProcessor(tmp_path / "jobs.db")
    job = Job(
        job_id=generate_id(),
        type="event",
        agent="general",
        task="submit the application",
        scheduled_at=utcnow(),
        max_retries=0,
    )
    await processor.enqueue(job)
    claimed = await processor.claim_next()
    assert claimed is not None

    async def _fail(_job: Job) -> None:
        raise RuntimeError("session expired mid-submit")

    await processor._run_job(claimed, _fail)

    stored = await processor.get(job.job_id)
    assert stored.status == JobStatus.NEEDS_ATTENTION
    assert stored.status != JobStatus.FAILED, "a half-failed submit is not simply failed"
