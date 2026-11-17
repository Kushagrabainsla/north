"""Which row of the Models panel each part of north follows (#80)."""

from __future__ import annotations

import pytest

from config.strategy import WorkKind
from inference.routing.parts import DEFAULT_PART_PROFILES, kind_of


@pytest.mark.parametrize(
    ("component", "kind"),
    [
        ("north", WorkKind.TASKS),
        ("an_agent_you_made", WorkKind.TASKS),
        ("", WorkKind.TASKS),
        ("critic", WorkKind.CHECKS),
        ("north_star_checker", WorkKind.CHECKS),
        ("approval_decider", WorkKind.APPROVALS),
        ("extraction_pipeline", WorkKind.BACKGROUND),
        ("extraction_pipeline:dedup", WorkKind.BACKGROUND),
        ("episode_consolidator", WorkKind.BACKGROUND),
        ("skill_distiller", WorkKind.BACKGROUND),
        ("fact_supersede", WorkKind.BACKGROUND),
        ("fact_glossary", WorkKind.BACKGROUND),
        ("fact_dates", WorkKind.BACKGROUND),
        ("context_injector", WorkKind.BACKGROUND),
        ("bootstrap", WorkKind.BACKGROUND),
        # Summaries inside another part's loop are background work, whoever runs them.
        ("north:compact", WorkKind.BACKGROUND),
        ("north:tool-summary", WorkKind.BACKGROUND),
        ("critic:compact", WorkKind.BACKGROUND),
        # A part merely named "compact" is not one running inside another.
        ("compact", WorkKind.TASKS),
    ],
)
def test_each_part_follows_its_kind(component: str, kind: WorkKind) -> None:
    assert kind_of(component) is kind


def test_every_part_with_a_profile_except_the_coder_is_named_as_a_kind() -> None:
    """A profiled part left out would follow Your tasks without anyone deciding it should."""
    unnamed = {part for part in DEFAULT_PART_PROFILES if kind_of(part) is WorkKind.TASKS}
    assert unnamed == {"coder"}


def test_every_profile_carries_its_own_part_label() -> None:
    for name, profile in DEFAULT_PART_PROFILES.items():
        assert profile.part == name, f"{name} carries the wrong part label"
        assert profile.order_by, f"{name} has no ranking axis"
