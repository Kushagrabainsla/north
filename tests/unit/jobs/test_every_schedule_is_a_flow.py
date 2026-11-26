"""Every schedule runs as a flow (#45): a job with no flow is not guessed at, it needs attention."""

from __future__ import annotations

import pytest

from jobs.exceptions import JobNeedsAttention
from jobs.models import Job, JobType
from orchestrator.app import flow_of
from utils.time import utcnow


def _job(payload: dict) -> Job:
    return Job(
        job_id="j1",
        type=JobType.CRON,
        agent="north",
        task="Compile the daily news briefing.",
        payload=payload,
        scheduled_at=utcnow(),
    )


def test_a_job_runs_the_flow_it_names() -> None:
    assert flow_of(_job({"flow": " daily-news-briefing "})) == "daily-news-briefing"


@pytest.mark.parametrize("payload", [{}, {"flow": ""}, {"skill": "news"}])
def test_a_job_with_no_flow_needs_attention_and_says_what_to_do(payload) -> None:
    with pytest.raises(JobNeedsAttention, match="has no flow") as raised:
        flow_of(_job(payload))

    assert "Compile the daily news briefing." in str(raised.value) and "Make it again" in str(raised.value)
