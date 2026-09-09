"""Approve/reject decisions kept as the training signal they are.

Issue #17. Every decision on prepared work is a labelled example of the user's
taste, on their own real data, for free - higher quality than fact extraction or
episodic summarisation, both of which north already invests heavily in. It was
being written to a status column and forgotten, so a flow could never improve.

The distinctions that carry the weight: an expiry is not a rejection, a
guard-rail is not evidence about taste, and the filter never learns to approve.
"""

from __future__ import annotations

import pytest

from approval.decisions import APPROVED, REJECTED, UNANSWERED, DecisionLog, card_summary
from approval.models import Card, CardField, CardType
from approval.prefilter import RejectionFilter


@pytest.fixture
def log(tmp_path) -> DecisionLog:
    return DecisionLog(tmp_path / "approval_memory.db")


def _card(title: str = "Staff Engineer at Acme", source: str = "job_applications", **kw) -> Card:
    defaults = {
        "type": CardType.APPROVAL,
        "agent": "general",
        "title": title,
        "message": "Apply?",
        "source": source,
        "blocking": False,
        "fields": [CardField(name="company", value="Acme")],
    }
    defaults.update(kw)
    return Card.new(**defaults)


# ── Recording ────────────────────────────────────────────────────────────────


def test_a_rejection_keeps_its_reason(log) -> None:
    """Without one a rejection says only "no", which cannot be learned from."""
    log.record(_card(), REJECTED, reason="Not senior enough")
    assert log.rejection_reasons("job_applications") == ["Not senior enough"]


def test_a_guard_rail_decision_is_not_recorded(log) -> None:
    """A question about one action in one task is not evidence about taste.

    Recording it would dilute the signal with data no flow can use.
    """
    log.record(Card.new(type=CardType.APPROVAL, agent="coder", title="Run it?", message="rm x"), REJECTED)
    assert log.all_stats() == []


def test_a_secret_in_a_reason_does_not_reach_the_log(log) -> None:
    log.record(_card(), REJECTED, reason="wrong account, api_key = sk_live_abcdef123456")
    assert "sk_live_abcdef123456" not in log.rejection_reasons("job_applications")[0]


# ── Approve rate ─────────────────────────────────────────────────────────────


def test_approve_rate_counts_only_what_you_answered(log) -> None:
    """An expiry means nobody was there.

    Folding it in makes a flow look bad for being scheduled while you slept.
    """
    log.record(_card("a"), APPROVED)
    log.record(_card("b"), REJECTED)
    log.record(_card("c"), UNANSWERED)

    stats = log.stats("job_applications")

    assert stats.offered == 3
    assert stats.decided == 2
    assert stats.approve_rate == 0.5
    assert stats.unanswered == 1


def test_a_source_with_nothing_answered_has_no_rate(log) -> None:
    log.record(_card(), UNANSWERED)
    assert log.stats("job_applications").approve_rate == 0.0


def test_the_worst_flow_is_reported_first(log) -> None:
    """A flow you reject 90% of the time is the one worth acting on."""
    log.record(_card("a", source="good"), APPROVED)
    log.record(_card("b", source="good"), APPROVED)
    log.record(_card("c", source="bad"), REJECTED)
    log.record(_card("d", source="bad"), REJECTED)

    assert [s.source for s in log.all_stats()] == ["bad", "good"]


def test_fields_you_always_rewrite_are_counted(log) -> None:
    """Rewriting the cover letter every time is a correction to the draft prompt."""
    for n in range(3):
        log.record(_card(f"job {n}"), APPROVED, edited_fields=["cover"])
    log.record(_card("job 4"), APPROVED, edited_fields=["cover", "salary"])

    assert log.most_edited_fields("job_applications")[0] == ("cover", 4)


# ── The pre-filter ───────────────────────────────────────────────────────────


class _Embedder:
    """Embeds by keyword, so similarity is predictable rather than a model's guess."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[1.0 if "junior" in t.lower() else 0.0, 1.0 if "staff" in t.lower() else 0.0, 0.1] for t in texts]


async def _seed(log: DecisionLog, rejected: int = 6, approved: int = 4) -> None:
    for n in range(rejected):
        log.record(_card(f"Junior Developer at Co{n}"), REJECTED, reason="too junior")
    for n in range(approved):
        log.record(_card(f"Staff Engineer at Co{n}"), APPROVED)


@pytest.mark.asyncio
async def test_a_candidate_like_your_rejections_is_filtered(log) -> None:
    await _seed(log)
    filt = RejectionFilter(log, _Embedder())

    verdict = await filt.verdict("job_applications", "Junior Developer at NewCo")

    assert verdict.filtered
    assert "rejected" in verdict.reason


@pytest.mark.asyncio
async def test_a_candidate_like_your_approvals_is_kept(log) -> None:
    await _seed(log)
    filt = RejectionFilter(log, _Embedder())

    assert not (await filt.verdict("job_applications", "Staff Engineer at NewCo")).filtered


@pytest.mark.asyncio
async def test_nothing_is_filtered_before_there_is_evidence(log) -> None:
    """Two rejections are a coincidence, not a pattern."""
    log.record(_card("Junior Developer"), REJECTED)
    filt = RejectionFilter(log, _Embedder())

    assert not (await filt.verdict("job_applications", "Junior Developer at NewCo")).filtered


@pytest.mark.asyncio
async def test_nothing_is_filtered_without_approvals_to_compare_against(log) -> None:
    """The threshold is a margin between two measurements, not an absolute score.

    An absolute cosine cutoff filters everything for a flow whose candidates all
    look alike and nothing for one whose candidates vary - the lesson already
    recorded in memory/embeddings.py.
    """
    for n in range(8):
        log.record(_card(f"Junior Developer {n}"), REJECTED)
    filt = RejectionFilter(log, _Embedder())

    assert not (await filt.verdict("job_applications", "Junior Developer at NewCo")).filtered


@pytest.mark.asyncio
async def test_an_embedding_failure_keeps_the_candidate(log) -> None:
    """Showing something unwanted costs seconds; hiding something wanted costs an opportunity."""

    async def _broken(texts):
        raise RuntimeError("no embedding model")

    await _seed(log)
    assert not (await RejectionFilter(log, _broken).verdict("job_applications", "Junior Developer")).filtered


@pytest.mark.asyncio
async def test_with_no_embeddings_at_all_nothing_is_filtered(log) -> None:
    await _seed(log)
    assert not (await RejectionFilter(log, None).verdict("job_applications", "Junior Developer")).filtered


# ── Auto-rejection stays auditable ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_what_was_filtered_can_be_inspected(log) -> None:
    """Auto-rejection is only acceptable if you can check what it threw away."""
    await _seed(log)
    filt = RejectionFilter(log, _Embedder())

    offered = await filt.should_offer("job_applications", "cand-1", "Junior Developer at NewCo")

    assert offered is False
    dropped = log.filtered("job_applications")
    assert len(dropped) == 1 and "Junior Developer" in dropped[0]["summary"]


@pytest.mark.asyncio
async def test_a_wrong_auto_rejection_can_be_undone(log) -> None:
    await _seed(log)
    await RejectionFilter(log, _Embedder()).should_offer("job_applications", "cand-1", "Junior Developer")

    assert log.unfilter("cand-1")
    assert log.filtered("job_applications") == []


def test_the_filter_has_no_way_to_approve() -> None:
    """Auto-rejection costs a missed opportunity; auto-approval cannot be undone.

    Pinned deliberately: however high an approve rate goes, nothing here should
    ever mature into "north submits without asking".
    """
    assert not [name for name in dir(RejectionFilter) if "approve" in name.lower()]


def test_a_card_summary_is_what_the_item_is(log) -> None:
    """Compared on the item, not on how the card happened to be worded."""
    summary = card_summary(_card())
    assert "Staff Engineer at Acme" in summary and "Acme" in summary
