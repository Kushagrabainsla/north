"""How the job queue is described to the dashboard."""

from __future__ import annotations

from jobs.models import Job, JobType
from orchestrator.api.jobs import _job_to_out
from utils.time import utcnow


def _job(**payload) -> Job:
    return Job(
        job_id="j1",
        type=JobType.CRON,
        agent="general",
        task="Compile and save the daily news briefing.",
        payload=payload,
        scheduled_at=utcnow(),
    )


def test_a_flows_firing_names_the_flow_it_runs() -> None:
    out = _job_to_out(_job(cron_entry="news_daily_briefing", flow="daily-news-briefing"))

    assert out.flow == "daily-news-briefing"
    assert out.cron_entry == "news_daily_briefing"


def test_a_prompt_only_job_names_no_flow() -> None:
    assert _job_to_out(_job(cron_entry="stretch")).flow == ""
