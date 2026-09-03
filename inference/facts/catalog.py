"""The refresh pipeline, and the in-memory snapshot routing actually reads.

On each refresh:

1. provider catalogs   -> endpoint rows (which models exist where, and their terms)
2. OpenRouter /models  -> facts + endpoint rows + measured scores
3. LiteLLM json        -> facts (daily, from disk cache)
4. canonicalise every id
5. merge facts by rank -> persist with provenance
6. upsert endpoints    -> price and limits from the serving provider only
7. rebuild each part's chain, bump the generation

**The hot path never touches SQLite.** A chain changes only when the catalog
does, so the database is the durable store and the offline cache while the
in-memory snapshot is what every inference call walks. Adding a query per LLM
call, for data that changes every three minutes, would be a regression.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from inference.capability import ModelInfo
from inference.facts.merge import (
    CONTRADICTION_THRESHOLD,
    ScorePrior,
    build_prior,
    contradict,
    merge_all,
    percentile_floor,
)
from inference.facts.models import Endpoint, Entitlement, ModelFacts
from inference.facts.sources import catalog as provider_catalog
from inference.facts.sources import openrouter as openrouter_source
from inference.facts.sources.litellm import LiteLLMSource
from inference.facts.store import ModelFactsStore
from inference.routing.chain import Candidate, Requirements, build_chain
from inference.routing.parts import PartProfile

logger = logging.getLogger(__name__)

# How many of a chain's leading models get their per-upstream detail fetched.
# /endpoints is one request per model, so only the models a walk actually
# reaches are worth the call.
ENDPOINT_DETAIL_DEPTH: int = 8


@runtime_checkable
class CatalogFactSource(Protocol):
    """A provider that publishes more than a list of ids.

    Implementing this is how a provider contributes real facts instead of just
    endpoint rows; a provider that does not implement it still works, its models
    simply borrow their facts from the sources that do describe them.
    """

    def raw_catalog(self) -> list[dict] | None:
        """The last raw ``/models`` payload this provider fetched, if any."""
        ...

    async def fetch_model_endpoints(self, model_id: str) -> dict | None:
        """Per-upstream detail for one model, or None when unavailable."""
        ...


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    """An immutable view of the catalog. Routing reads only this."""

    facts: dict[str, ModelFacts] = field(default_factory=dict)
    endpoints_by_model: dict[str, list[Endpoint]] = field(default_factory=dict)
    generation: int = 0
    updated_at: datetime | None = None

    @property
    def is_empty(self) -> bool:
        return not self.facts or not self.endpoints_by_model

    def endpoints_of(self, canonical_id: str) -> list[Endpoint]:
        return self.endpoints_by_model.get(canonical_id, [])


class FactsCatalog:
    """Fetches, merges, persists and serves model facts.

    Owns the per-part chain cache: a chain is pure over (catalog, profile), so it
    is built once per refresh and only *narrowed* per call.
    """

    def __init__(
        self,
        store: ModelFactsStore,
        *,
        litellm: LiteLLMSource | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._litellm = litellm or LiteLLMSource(
            (cache_dir or store.db_path.parent / "cache") / "litellm_models.json"
        )
        self._snapshot = CatalogSnapshot()
        # Keyed on the whole profile, not just the part name: the power dial can
        # reshape a part's ordering, and two orderings are two chains.
        self._chains: dict[tuple[PartProfile, int], list[Candidate]] = {}
        self._priors: dict[tuple[str, int], ScorePrior] = {}
        self._floors: dict[tuple[str, float, int], float] = {}
        self._generation = 0
        self._endpoint_detail_done: set[str] = set()
        # (canonical_id, field) -> the endpoints that have contradicted it. A
        # capability is only demoted once independent endpoints agree.
        self._contradictions: dict[tuple[str, str], set[str]] = {}
        self.load()

    # ---- snapshot ----

    @property
    def snapshot(self) -> CatalogSnapshot:
        return self._snapshot

    def load(self) -> CatalogSnapshot:
        """Rebuild the snapshot from disk. This is what makes north work offline."""
        try:
            facts = self._store.load_facts()
            endpoints = self._store.load_endpoints()
        except Exception:
            logger.warning("Could not read %s - starting with no facts", self._store.db_path, exc_info=True)
            return self._snapshot
        if facts or endpoints:
            self._publish(facts, endpoints)
        return self._snapshot

    def _publish(self, facts: dict[str, ModelFacts], endpoints: Iterable[Endpoint]) -> None:
        grouped: dict[str, list[Endpoint]] = {}
        for endpoint in endpoints:
            grouped.setdefault(endpoint.canonical_id, []).append(endpoint)
        self._generation += 1
        self._chains.clear()
        self._priors.clear()
        self._floors.clear()
        self._snapshot = CatalogSnapshot(
            facts=facts,
            endpoints_by_model=grouped,
            generation=self._generation,
            updated_at=datetime.now(UTC),
        )

    # ---- refresh ----

    def contradict(self, canonical_id: str, field: str, source: str) -> bool:
        """Record that a declared capability failed in practice; demote it at threshold.

        Observation contradicts, never asserts: this can only ever turn a declared
        ``true`` into an OBSERVED ``false``, and only once
        :data:`CONTRADICTION_THRESHOLD` *independent* endpoints have failed the
        same way. One failure is a bad gateway or a bad minute, not evidence about
        the model.

        Returns True when the fact was demoted.
        """
        record = self._snapshot.facts.get(canonical_id)
        if record is None or not record.declares(field) or not record.value(field):
            return False  # nothing declared it true, so there is nothing to contradict
        witnesses = self._contradictions.setdefault((canonical_id, field), set())
        witnesses.add(source)
        if len(witnesses) < CONTRADICTION_THRESHOLD:
            logger.info(
                "%s failed %s on %s (%d of %d independent failures needed to demote it)",
                canonical_id,
                field,
                source,
                len(witnesses),
                CONTRADICTION_THRESHOLD,
            )
            return False

        demoted = contradict(record, field, source)
        facts = dict(self._snapshot.facts)
        facts[canonical_id] = demoted
        try:
            self._store.upsert_facts([demoted])
        except Exception:
            logger.warning("Could not persist the contradiction for %s.%s", canonical_id, field, exc_info=True)
        logger.warning(
            "%s declared %s but failed it on %d providers - recording OBSERVED false",
            canonical_id,
            field,
            len(witnesses),
        )
        self._publish(facts, [e for group in self._snapshot.endpoints_by_model.values() for e in group])
        return True

    async def refresh(
        self,
        providers: Sequence[object],
        registry_models: dict[str, dict[str, ModelInfo]],
        configured_providers: Iterable[str] = (),
    ) -> None:
        """Run the pipeline. Never raises: a failed refresh keeps the last snapshot.

        *registry_models* is ``{provider_name: {model_id: ModelInfo}}`` as the
        dispatcher already has it, so no provider is asked for its catalog twice.
        *configured_providers* is every provider this install has credentials for,
        which is what distinguishes "its refresh failed" from "its key is gone".
        """
        try:
            await self._refresh(providers, registry_models, set(configured_providers))
        except Exception:
            logger.warning("Model-facts refresh failed - keeping the previous catalog", exc_info=True)

    async def _refresh(
        self,
        providers: Sequence[object],
        registry_models: dict[str, dict[str, ModelInfo]],
        configured_providers: set[str],
    ) -> None:
        fact_records: list[ModelFacts] = []
        endpoints: list[Endpoint] = []

        # 1. provider catalogs: which models exist where, on this account's terms.
        for models in registry_models.values():
            endpoints.extend(provider_catalog.endpoints_from_models(models))
            fact_records.extend(provider_catalog.inferred_facts(models))

        # 2. OpenRouter: the only source carrying measured coding/agentic scores.
        for provider in providers:
            if not isinstance(provider, CatalogFactSource):
                continue
            raw = provider.raw_catalog()
            if not raw:
                continue
            source_facts, source_endpoints = openrouter_source.facts_from_catalog(raw)
            fact_records.extend(source_facts)
            endpoints.extend(source_endpoints)

        # 3. LiteLLM: describes models no other source does, the codex line included.
        fact_records.extend(await self._litellm.load())

        # 4-6. merge by rank, then persist. Endpoint rows keep their learned
        # entitlement; a catalog refresh knows nothing about the account.
        merged = merge_all(fact_records)
        deduped = _dedupe_endpoints(endpoints)
        live_providers = sorted(registry_models.keys())
        merged = self._reapply_contradictions(merged)
        try:
            self._store.upsert_facts(merged.values())
            self._store.upsert_endpoints(deduped)
            self._store.prune_missing_endpoints(
                live_providers, {e.key for e in deduped if e.provider in registry_models}
            )
            if configured_providers:
                self._store.drop_unconfigured_providers(configured_providers)
        except Exception:
            logger.warning("Could not persist model facts - continuing in memory", exc_info=True)

        # 7. Republish from the store so persisted entitlements come back with it.
        stored_endpoints = self._safe_load_endpoints(deduped)
        self._publish(merged, stored_endpoints)
        logger.info(
            "Model facts refreshed: %d models, %d endpoints, %d with measured coding scores",
            len(merged),
            len(stored_endpoints),
            sum(1 for record in merged.values() if record.value("coding_score") is not None),
        )
        await self._refresh_endpoint_detail(providers)

    async def _refresh_endpoint_detail(self, providers: Sequence[object]) -> None:
        """Fetch per-upstream detail for the models chains actually reach.

        Only the head of the coder chain is worth a request each: that is the part
        that reaches the most expensive models, where the cheapest upstream and a
        quantization disagreement matter most.
        """
        from inference.routing.parts import profile_for

        try:
            head = self.chain_for(profile_for("coder"))
        except Exception:  # pragma: no cover - detail is an enrichment, never a dependency
            return
        for provider in providers:
            await self.fetch_endpoint_detail(provider, head)

    def _reapply_contradictions(self, merged: dict[str, ModelFacts]) -> dict[str, ModelFacts]:
        """Re-assert what north has watched fail, over what the sources re-declare.

        A refresh re-reads the same declarations that were already contradicted,
        and rank alone would let them win back - OBSERVED outranks DECLARED, but
        only if the observation is still in the record.
        """
        for (canonical_id, fact_field), witnesses in self._contradictions.items():
            record = merged.get(canonical_id)
            if record is None or len(witnesses) < CONTRADICTION_THRESHOLD:
                continue
            merged[canonical_id] = contradict(record, fact_field, ",".join(sorted(witnesses)))
        return merged

    def _safe_load_endpoints(self, fallback: list[Endpoint]) -> list[Endpoint]:
        try:
            stored = self._store.load_endpoints()
        except Exception:
            return fallback
        return stored or fallback

    async def fetch_endpoint_detail(self, provider: object, chain: Sequence[Candidate]) -> None:
        """Enrich the head of *chain* with per-upstream price, uptime and quantization.

        One request per model, so only the models a walk plausibly reaches are
        worth it, and each is fetched once per process.
        """
        if not isinstance(provider, CatalogFactSource):
            return
        updated: list[Endpoint] = []
        for candidate in chain[:ENDPOINT_DETAIL_DEPTH]:
            for endpoint in candidate.endpoints:
                if endpoint.provider != openrouter_source.SOURCE:
                    continue
                if endpoint.provider_model_id in self._endpoint_detail_done:
                    continue
                self._endpoint_detail_done.add(endpoint.provider_model_id)
                payload = await provider.fetch_model_endpoints(endpoint.provider_model_id)
                if payload:
                    updated.append(openrouter_source.enrich_from_endpoints(endpoint, payload))
        if updated:
            self._store.upsert_endpoints(updated)
            self._publish(self._snapshot.facts, self._safe_load_endpoints([]))

    # ---- chains ----

    def prior_for(self, score_field: str) -> ScorePrior:
        key = (score_field, self._snapshot.generation)
        prior = self._priors.get(key)
        if prior is None:
            prior = build_prior(self._snapshot.facts.values(), self._snapshot.endpoints_by_model, score_field)
            self._priors[key] = prior
        return prior

    def floor_for(self, score_field: str, percentile: float) -> float:
        key = (score_field, percentile, self._snapshot.generation)
        floor = self._floors.get(key)
        if floor is None:
            floor = percentile_floor(self._snapshot.facts.values(), score_field, percentile)
            self._floors[key] = floor
        return floor

    def chain_for(self, profile: PartProfile, demoted: Callable[[str], bool] | None = None) -> list[Candidate]:
        """This part's full chain, built once per catalog generation.

        When the profile's own preferences (a 200k context floor, a quality
        floor) would leave nothing, they are relaxed rather than obeyed into an
        empty chain: a preference about model class must never be the reason a
        task cannot run. What the *request* needs is applied afterwards, by
        ``narrow``, and is never relaxed.
        """
        key = (profile, self._snapshot.generation)
        cached = self._chains.get(key)
        if cached is not None:
            return cached

        score_field = profile.order_by if profile.ranks_by_score else profile.floor_field
        prior = self.prior_for(score_field)
        floor = (
            self.floor_for(score_field, profile.quality_floor_percentile)
            if profile.quality_floor_percentile is not None
            else None
        )
        requirements = Requirements(capabilities=profile.requires, min_context=profile.min_context)
        chain = build_chain(
            profile,
            requirements,
            self._snapshot.facts,
            self._snapshot.endpoints_by_model,
            prior,
            floor=floor,
            demoted=demoted,
        )
        if not chain and (profile.min_context or floor is not None):
            logger.info(
                "No model meets the %s profile's preferences - relaxing them for this catalog", profile.part
            )
            chain = build_chain(
                profile,
                Requirements(capabilities=profile.requires),
                self._snapshot.facts,
                self._snapshot.endpoints_by_model,
                prior,
                demoted=demoted,
            )
        self._chains[key] = chain
        return chain

    def entitlement_updates(self, provider: str, entitlement: Entitlement, *, paid_only: bool) -> int:
        """Persist what a live call proved about this account's access to *provider*."""
        keys = [
            endpoint.key
            for endpoints in self._snapshot.endpoints_by_model.values()
            for endpoint in endpoints
            if endpoint.provider == provider and (not paid_only or not endpoint.is_free)
        ]
        try:
            return self._store.set_entitlement(keys, entitlement)
        except Exception:
            logger.warning("Could not persist entitlement for %s", provider, exc_info=True)
            return 0


def _dedupe_endpoints(endpoints: Iterable[Endpoint]) -> list[Endpoint]:
    """One row per (model, provider, provider_model_id); the last writer wins.

    Provider catalogs are read before the fetched sources, so a source's richer
    row overwrites the bare one for the same endpoint.
    """
    merged: dict[tuple[str, str, str], Endpoint] = {}
    for endpoint in endpoints:
        merged[endpoint.key] = endpoint
    return list(merged.values())
