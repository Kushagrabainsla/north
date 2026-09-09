"""The chain each part of a task would walk, as the System page shows it.

This replaced a "model pools" panel that displayed capability buckets left over
from the router that was deleted. Grouping is not selection: routing ranks a
chain per part, on the axis that part is ranked by, and walks it in order.
"""

from __future__ import annotations

from inference.base import InferenceRouter


class _NoChains(InferenceRouter):
    """A router that does not route by chains - it must still answer."""

    async def complete(self, request):  # pragma: no cover - not exercised
        raise NotImplementedError

    async def complete_with_tools(self, request, token_callback=None):  # pragma: no cover
        raise NotImplementedError

    async def embed(self, request):  # pragma: no cover
        raise NotImplementedError

    async def refresh_pools(self):  # pragma: no cover
        raise NotImplementedError

    async def transcribe(self, request):  # pragma: no cover
        raise NotImplementedError

    def current_pools(self):
        return {}


def test_a_router_without_chains_answers_with_nothing_to_show() -> None:
    """Concrete default, not an abstract stub every router must write.

    The web layer had a `hasattr` guard for this, which is the shape of a
    question the interface should have answered.
    """
    assert _NoChains().part_chains() == []


def test_the_dispatcher_reports_nothing_until_the_catalog_loads() -> None:
    """Routing that is not ready is reported as empty, never as a crash.

    The panel is polled from the page, so this runs during startup on every
    boot - before the catalog has been fetched.
    """
    from inference.dispatcher import ModelDispatcher

    dispatcher = ModelDispatcher.__new__(ModelDispatcher)
    dispatcher._chain_router = None  # noqa: SLF001 - the pre-catalog state

    assert dispatcher.part_chains() == []


def test_every_part_north_ships_is_described() -> None:
    """The panel lists parts, so a part with no profile would be invisible.

    Asserted against the profile table rather than a copy of it here, so adding
    a part cannot silently leave it off the page.
    """
    from inference.routing.parts import DEFAULT_PART_PROFILES

    assert "coder" in DEFAULT_PART_PROFILES
    assert "planner" in DEFAULT_PART_PROFILES
    for name, profile in DEFAULT_PART_PROFILES.items():
        assert profile.part == name, f"{name} carries the wrong part label"
        # Both are rendered on the page; neither may be empty.
        assert profile.order_by, f"{name} has no ranking axis"
