"""What each part of a task needs from a model, and what it should be ranked by.

Every north LLM call already carries a ``component`` label - it has been
threaded through for cost attribution and stickiness since the router was
written. It has never been used to *pick* the model, so the coder and the
compaction pass that runs inside the coder's own loop competed for the same
frontier model. Here that label becomes the profile key, so the coder gets the
strongest model while its own summaries run free. No new plumbing: the label was
already there.

Profiles are data. The defaults below are overridable per install through a
``routing`` key in ``settings.json``, and requirements the profile does not state
are derived from the request itself - a call carrying tools needs tool support, a
47k-token prompt needs a 47k window. The caller never names a pool.

Completions and tool calls route this way. Embedding and transcription do not:
no catalog source declares either capability, so those two keep selecting from
the provider registry's own capability flags, which is the only place that
knowledge exists.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

# Capability requirement names. These are the ModelFacts boolean fields, minus
# the "supports_" prefix, so a profile reads as prose.
TOOLS = "tools"
REASONING = "reasoning"
STRUCTURED = "structured"
VISION = "vision"

REQUIREMENT_TO_FIELD: dict[str, str] = {
    TOOLS: "supports_tools",
    REASONING: "supports_reasoning",
    STRUCTURED: "supports_structured",
}


class Order:
    """How a chain is ordered. One axis, never a blend of quality and cost.

    Blending the two into a single score silently trades correctness for money
    at a rate nobody chose. Cost enters as a filter (``max_price``) and as the
    tie-break between models of equal measured quality - never as a term.
    """

    CODING = "coding_score"
    AGENTIC = "agentic_score"
    INTELLIGENCE = "intelligence_score"
    CHEAPEST = "cheapest"


@dataclass(frozen=True, slots=True)
class PartProfile:
    """The routing policy for one part of a task."""

    part: str
    requires: frozenset[str] = frozenset()
    min_context: int = 0
    order_by: str = Order.INTELLIGENCE
    # Hard cost ceiling (USD per output token). None means the chain may reach
    # anything - the task completes and the decision log shows what it cost.
    max_price: float | None = None
    # For cheapest-first parts: how good a model must still be, expressed as a
    # percentile of the catalog's own score distribution rather than an absolute
    # number, so it keeps meaning the same thing as the catalog changes.
    quality_floor_percentile: float | None = None
    # The score field the floor is measured against.
    floor_field: str = Order.INTELLIGENCE
    # Manual override: a model spec that is tried first regardless of rank.
    pinned_model: str | None = None

    @property
    def ranks_by_score(self) -> bool:
        return self.order_by != Order.CHEAPEST


# Suffix marking a part that runs *inside* another part's loop. Compaction is the
# case that matters: it fires several times per task from within the coder's own
# ReAct loop, and inheriting the coder's model meant paying frontier prices to
# summarise scratch history.
NESTED_SEPARATOR = ":"
COMPACT_SUFFIX = "compact"

_CODING_PARTS = ("coder", "reviewer", "tester")
_DESIGN_PARTS = ("architect", "spec_critique")
_TINY_STRUCTURED_PARTS = ("critic", "north_star_checker", "judgement_filter")
_BACKGROUND_PARTS = ("extraction_pipeline", "episode_consolidator", "skill_distiller")
_PROSE_PARTS = ("synthesizer", "context_injector")

# The context a serious coding task needs before it is worth starting.
_CODING_CONTEXT = 200_000
# Floors as percentiles of the live score distribution: a cheap part must still
# land in the upper half of what the catalog offers, and a background part need
# only clear the bottom third.
_GATING_FLOOR = 0.50
_BACKGROUND_FLOOR = 0.30

DEFAULT_PART_PROFILES: dict[str, PartProfile] = {
    # The expensive one. Everything else exists so this can have the best model.
    "coder": PartProfile("coder", frozenset({TOOLS}), _CODING_CONTEXT, Order.CODING),
    # Ranked identically to the coder; independence comes from exclude_models,
    # which the chain honours as a hard filter rather than a preference.
    "reviewer": PartProfile("reviewer", frozenset({TOOLS}), 0, Order.CODING),
    "tester": PartProfile("tester", frozenset({TOOLS}), 0, Order.CODING),
    "architect": PartProfile("architect", frozenset({REASONING}), 0, Order.INTELLIGENCE),
    "spec_critique": PartProfile("spec_critique", frozenset({REASONING}), 0, Order.INTELLIGENCE),
    # Reads a lot, so it needs the window; cost enters only as the tie-break.
    "researcher": PartProfile("researcher", frozenset({TOOLS}), _CODING_CONTEXT, Order.AGENTIC),
    # Small JSON, but it gates every task - cheap, yet not from the bottom.
    "planner": PartProfile(
        "planner", frozenset({STRUCTURED}), 0, Order.CHEAPEST, quality_floor_percentile=_GATING_FLOOR
    ),
    "synthesizer": PartProfile(
        "synthesizer", frozenset(), 0, Order.CHEAPEST, quality_floor_percentile=_GATING_FLOOR
    ),
}
DEFAULT_PART_PROFILES.update(
    {
        part: PartProfile(
            part, frozenset({STRUCTURED}), 0, Order.CHEAPEST, quality_floor_percentile=_GATING_FLOOR
        )
        for part in _TINY_STRUCTURED_PARTS
    }
)
DEFAULT_PART_PROFILES.update(
    {
        part: PartProfile(
            part, frozenset({STRUCTURED}), 0, Order.CHEAPEST, quality_floor_percentile=_BACKGROUND_FLOOR
        )
        for part in _BACKGROUND_PARTS
    }
)
DEFAULT_PART_PROFILES["context_injector"] = PartProfile(
    "context_injector", frozenset(), 0, Order.CHEAPEST, quality_floor_percentile=_GATING_FLOOR
)

# Anything not named above: requirements come entirely from the request, ranked
# by general intelligence. A safe default for a new call site that nobody has
# thought about yet, and for every user-created agent.
FALLBACK_PROFILE = PartProfile("(unknown)", frozenset(), 0, Order.INTELLIGENCE)

# High volume, runs inside another part's loop, and summarises text that has
# already been produced. The cheapest thing that can write a paragraph will do.
COMPACT_PROFILE = PartProfile("(compact)", frozenset(), 0, Order.CHEAPEST)


def profile_for(component: str, overrides: dict[str, PartProfile] | None = None) -> PartProfile:
    """The profile for a ``component`` label, honouring install overrides.

    ``coder:compact`` resolves to the compaction profile rather than the coder's,
    which is the entire point of keying on the label: a part running inside
    another part's loop is a different part.
    """
    label = (component or "").strip()
    table = {**DEFAULT_PART_PROFILES, **(overrides or {})}
    if label in table:
        return table[label]
    base, sep, suffix = label.rpartition(NESTED_SEPARATOR)
    if sep and suffix == COMPACT_SUFFIX:
        return table.get(label, replace(COMPACT_PROFILE, part=label))
    if sep and base in table:
        # Another nested part (e.g. "extraction_pipeline:dedup") - inherit the
        # parent's policy rather than falling all the way back.
        return replace(table[base], part=label)
    return replace(FALLBACK_PROFILE, part=label or "(unknown)")


def with_power(profile: PartProfile, power: str | None) -> PartProfile:
    """Apply the user's power dial to a part's profile.

    The dial predates chains and its own redesign is parked, but it is a live
    user-facing control - "switch to eco mode" - so it keeps meaning what it
    always meant, expressed against a chain instead of a pool:

    ``eco``     order every part cheapest-first, keeping its quality floor
    ``cruise``  the part's own profile (default)
    ``sport``   order every part on quality, ignoring a cheapest-first profile

    Requirements are never touched: the dial is about ordering, and a part that
    needs tools still needs tools however the dial is set.
    """
    mode = (power or "").strip().lower()
    if mode == "eco" and profile.ranks_by_score:
        return replace(profile, order_by=Order.CHEAPEST, floor_field=profile.order_by)
    if mode == "sport" and not profile.ranks_by_score:
        return replace(profile, order_by=profile.floor_field, quality_floor_percentile=None)
    return profile


# What an agent's configured ``model_pool`` asks for, expressed as an ordering.
# Pools are gone as a selection mechanism, but ``create_agent`` still offers the
# setting and agent configs still carry it, so the intent behind it is honoured
# rather than silently dropped.
_POOL_ORDERS: dict[str, str] = {
    "reasoning": Order.INTELLIGENCE,
    "speed": Order.CHEAPEST,
    "fast_cheap": Order.CHEAPEST,
    "high_volume": Order.CHEAPEST,
}


def with_pool(profile: PartProfile, pool: str | None) -> PartProfile:
    """Apply a caller's ``model_pool`` to a part's profile as an ordering hint.

    Only the *direction* carries over, and only where the pool actually disagrees
    with the part: ``fast_cheap`` / ``high_volume`` make a quality-ranked part
    cost-ranked, and ``reasoning`` makes a cost-ranked part quality-ranked.

    A pool must never restate which quality axis a part uses. ``reasoning`` is the
    default in every agent config, so treating it as "rank on intelligence" would
    quietly take the coder off its measured coding score on every single call.
    Requirements are never touched either way.
    """
    order = _POOL_ORDERS.get((pool or "").strip().lower())
    if order is None:
        return profile
    if order == Order.CHEAPEST and profile.ranks_by_score:
        return replace(profile, order_by=Order.CHEAPEST, floor_field=profile.order_by)
    if order != Order.CHEAPEST and not profile.ranks_by_score:
        return replace(profile, order_by=profile.floor_field, quality_floor_percentile=None)
    return profile


def parse_profiles(raw: object) -> dict[str, PartProfile]:
    """Coerce a settings.json ``routing.parts`` value into profiles, dropping junk.

    A malformed override degrades to the built-in default for that part rather
    than raising - a typo in settings must not stop north from routing.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, PartProfile] = {}
    for part, spec in raw.items():
        if not isinstance(part, str) or not isinstance(spec, dict):
            continue
        base = DEFAULT_PART_PROFILES.get(part, replace(FALLBACK_PROFILE, part=part))
        requires = spec.get("requires")
        try:
            out[part] = replace(
                base,
                part=part,
                requires=frozenset(str(r) for r in requires) if isinstance(requires, list) else base.requires,
                min_context=int(spec.get("min_context", base.min_context)),
                order_by=str(spec.get("order_by", base.order_by)),
                max_price=(None if spec.get("max_price") is None else float(spec["max_price"])),
                quality_floor_percentile=(
                    None
                    if spec.get("quality_floor_percentile") is None
                    else float(spec["quality_floor_percentile"])
                ),
                pinned_model=(str(spec["pinned_model"]) if spec.get("pinned_model") else None),
            )
        except (TypeError, ValueError):
            continue
    return out
