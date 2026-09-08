"""Stand in for the network refresh a dispatcher does after it is constructed.

A ``ModelDispatcher`` fetches model facts over the network, so a freshly built
one has no catalog and routes nothing - which is correct, and useless for a test
that wants to watch a call being routed. ``publish_catalog`` derives a catalog
from what the test's own fake providers already declare, so a test describes its
models once.
"""

from __future__ import annotations

from datetime import UTC, datetime

from inference.facts.identity import canonical
from inference.facts.models import Endpoint, ModelFacts, Rank, fact

_WHEN = datetime(2026, 9, 3, tzinfo=UTC)


def publish_catalog(dispatcher, scores: dict[str, float] | None = None) -> None:
    """Publish facts for every model in *dispatcher*'s registry.

    ``scores`` overrides the ranking score per model id; anything unnamed is
    ranked by the ``base_quality`` its ``ModelInfo`` already carries, which is
    what these tests were expressing before ranking moved to fetched facts.
    """
    router = dispatcher._chain_router  # noqa: SLF001 - the test is the refresh
    assert router is not None, "dispatcher was built without a models.db path"
    facts: dict[str, ModelFacts] = {}
    endpoints: list[Endpoint] = []
    for info, _provider in dispatcher._registry.values():
        canonical_id = canonical(info.model_id)
        score = (scores or {}).get(info.model_id, info.base_quality)
        where = info.provider_name
        facts[canonical_id] = ModelFacts(
            canonical_id,
            context_window=fact(info.context_window or 400_000, Rank.DECLARED, where, _WHEN),
            supports_tools=fact(True, Rank.DECLARED, where, _WHEN),
            supports_structured=fact(True, Rank.DECLARED, where, _WHEN),
            coding_score=fact(score, Rank.DECLARED, where, _WHEN),
            intelligence_score=fact(score, Rank.DECLARED, where, _WHEN),
        )
        endpoints.append(
            Endpoint(
                canonical_id,
                info.provider_name,
                info.model_id,
                info.cost_per_token,
                info.cost_per_token,
                max_payload_chars=info.max_payload_chars,
            )
        )
    router.catalog._publish(facts, endpoints)  # noqa: SLF001 - as above
