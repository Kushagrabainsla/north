"""What north has already offered you, so it does not offer it again tomorrow.

A daily flow finds the same candidates every day. Without this it re-proposes
them forever - the most likely way the review queue becomes annoying enough to
turn off. Raw URL matching does not work, which is what most of these are about.
"""

from __future__ import annotations

import pytest

from jobs.seen import SeenStore, natural_key, normalize_key


@pytest.fixture
def store(tmp_path) -> SeenStore:
    return SeenStore(tmp_path / "jobs.db")


def test_the_same_item_is_not_offered_twice(store) -> None:
    store.remember("job_applications", "https://acme.com/jobs/1")
    assert store.seen("job_applications", "https://acme.com/jobs/1")


def test_a_different_item_is_still_offered(store) -> None:
    store.remember("job_applications", "https://acme.com/jobs/1")
    assert not store.seen("job_applications", "https://acme.com/jobs/2")


def test_sources_do_not_share_a_memory(store) -> None:
    store.remember("job_applications", "https://acme.com/jobs/1")
    assert not store.seen("papers_to_read", "https://acme.com/jobs/1")


@pytest.mark.parametrize(
    "variant",
    [
        "https://acme.com/jobs/1?utm_source=linkedin",
        "https://acme.com/jobs/1/",
        "https://www.acme.com/jobs/1",
        "HTTPS://Acme.com/jobs/1",
        "https://acme.com/jobs/1?gclid=xyz&fbclid=abc",
    ],
)
def test_tracking_parameters_and_casing_do_not_make_it_a_new_item(store, variant: str) -> None:
    """One posting arrives many ways. Raw URL dedupe fails on every one of these."""
    store.remember("job_applications", "https://acme.com/jobs/1")
    assert store.seen("job_applications", variant), variant


def test_a_meaningful_query_parameter_is_kept(store) -> None:
    """Stripping everything would collapse two genuinely different pages."""
    store.remember("job_applications", "https://acme.com/jobs?id=1")
    assert not store.seen("job_applications", "https://acme.com/jobs?id=2")


def test_the_same_role_cross_posted_elsewhere_is_recognised(store) -> None:
    """No amount of URL normalising catches this; the natural key does."""
    store.remember(
        "job_applications",
        "https://linkedin.com/jobs/999",
        natural_key("Acme", "Staff Engineer"),
    )

    assert store.seen(
        "job_applications",
        "https://greenhouse.io/acme/staff-eng",
        natural_key("acme", "staff engineer"),
    )


def test_a_wrong_dedupe_can_be_undone(store) -> None:
    store.remember("job_applications", "https://acme.com/jobs/1")
    assert store.forget("job_applications", "https://acme.com/jobs/1")
    assert not store.seen("job_applications", "https://acme.com/jobs/1")


def test_an_empty_key_matches_nothing(store) -> None:
    """Otherwise one item with a missing URL suppresses every later item."""
    store.remember("job_applications", "")
    assert not store.seen("job_applications", "")
    assert store.count("job_applications") == 0


def test_normalising_is_stable(store) -> None:
    assert normalize_key("https://WWW.Acme.com/Jobs/1/?utm_source=x") == normalize_key("https://acme.com/jobs/1")
