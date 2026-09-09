"""Turning a decided card into the work that follows it.

`approval/continuation.py` says a resolved card emits its decision and whatever
created the card decides what happens next. This is the ready-made handler for
the common answer to "what next": run a prompt as a job, with the values as the
user decided them.

It lives in `jobs/` rather than `approval/` on purpose. The approval layer is
the consent boundary and knows nothing about jobs; this is the other side of
that line.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from approval.continuation import CardOutcome
from jobs.base import JobProcessor
from jobs.models import Job, JobPriority, JobStatus, JobType
from utils.ids import generate_id
from utils.time import utcnow

logger = logging.getLogger(__name__)


def render(template: str, values: Mapping[str, object]) -> str:
    """Fill *template* from the decided values.

    `str.format_map` with a defaulting mapping, so a template naming a field the
    card did not carry renders a visible placeholder instead of raising. A
    KeyError here would fail the follow-up to a decision the user already made,
    which they cannot act on and would never see.
    """

    class _Defaulting(dict):
        def __missing__(self, key: str) -> str:
            logger.warning("Next-step template referenced unknown field %r", key)
            return f"({key} not provided)"

    return template.format_map(_Defaulting(values))


def enqueue_next_step(
    processor: JobProcessor,
    *,
    agent: str,
    prompt_template: str,
    sends_something: bool = False,
    priority: JobPriority = JobPriority.MEDIUM,
    on_decision: str = "approved",
):
    """Build a continuation that enqueues one job when a card is decided *on_decision*.

    `sends_something` is the important argument. A step that submits, sends or
    posts on your behalf gets `max_retries = 0`, because those half-fail
    constantly - session expired, captcha, submit clicked but the response lost
    - and a retry then applies twice. Applying twice is worse than not applying.
    Its failure lands in `needs_attention` for you to look at, rather than being
    repeated or buried.

    Note this is a property of the *step*, not of the decision: submitting is
    what is unsafe to repeat, and approving is not.
    """

    async def _next_step(outcome: CardOutcome) -> None:
        if outcome.decision != on_decision:
            return
        job = Job(
            job_id=generate_id(),
            type=JobType.EVENT,
            agent=agent,
            task=render(prompt_template, outcome.values),
            payload={
                "card_id": outcome.card.id,
                "source": outcome.source,
                "decision": outcome.decision,
                "chosen_option": outcome.chosen_option,
                # The values as decided, after any edits - the whole point of
                # returning the card rather than a bool.
                "values": dict(outcome.values),
            },
            status=JobStatus.PENDING,
            priority=priority,
            scheduled_at=utcnow(),
            max_retries=0 if sends_something else 3,
        )
        await processor.enqueue(job)
        logger.info(
            "Card %s (%s) enqueued job %s; retries=%d",
            outcome.card.id,
            outcome.decision,
            job.job_id,
            job.max_retries,
        )

    return _next_step
