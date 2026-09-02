"""Task-shaped model tiering: which model pool a task's agent runs should use.

The tier follows the *work*, not the agent. The same coder fixing a typo and
designing a caching layer should not cost the same, so the decision is made from
the shape of the plan the planner already produces - its `engineering_kind`,
execution path, mode, and confidence - rather than hardcoded per agent.

Deliberately conservative. Only short, low-branching work is sent to the cheap
pool: `ModelDispatcher` cools down models that return degenerate or unparseable
responses, so a weak model driving a 20-turn ReAct loop can cost more in retries
than the tier saves. Anything large, multi-stage, or uncertain stays on reasoning.

The power dial (eco/cruise/sport) is NOT applied here - `ModelDispatcher`
`_effective_priority` already forces the pool for eco and sport at dispatch time.
This module only answers "what shape of work is this?".
"""

from __future__ import annotations

from orchestrator.models import ExecutionMode, ExecutionPath, ExecutionPlan

# The three pools a task can land in, cheapest first. These are canonical pool
# names from `inference/models.py:POOL_NAMES`; `POOL_TO_PRIORITY` maps them onto
# the PoolPriority the dispatcher ranks candidates by.
CHEAP = "high_volume"
MID = "speed"
BEST = "reasoning"

# Engineering kinds whose work is short and low-branching enough for the cheap
# pool: answering a question about the code, or writing tests against code that
# already exists.
_CHEAP_KINDS: frozenset[str] = frozenset({"question", "test"})

# Localized changes and shipping. Real work, but bounded in scope - a weak model
# is a genuine risk here, a mid-tier one is not.
_MID_KINDS: frozenset[str] = frozenset({"bugfix", "debug", "deploy", "ship", "research"})

# Open-ended design and restructuring. Always the best pool available.
_BEST_KINDS: frozenset[str] = frozenset({"feature", "refactor"})

# Below this planner confidence the plan itself is a guess, so don't compound it
# by also economising on the model.
_LOW_CONFIDENCE: float = 0.6

# Multi-agent shapes: coordination and handoffs punish a weak model.
_BEST_MODES: frozenset[ExecutionMode] = frozenset({ExecutionMode.PARALLEL, ExecutionMode.HIERARCHICAL})


def resolve_model_pool(plan: ExecutionPlan | None = None, domain: str = "", confidence: float = 1.0) -> str:
    """The pool this task's agent runs should use.

    Starts from the task's kind, then promotes: any signal that the work is
    large, multi-stage, or uncertain wins. Nothing demotes, so a cheap tier is
    only ever chosen when every signal agrees the work is small.
    """
    if plan is not None:
        if _needs_best(plan, confidence):
            return BEST
        if plan.mode == ExecutionMode.SINGLE_TOOL:
            return CHEAP  # one deterministic tool call, no reasoning to do
        kind = plan.engineering_kind.strip().lower()
        if kind in _BEST_KINDS:
            return BEST
        if kind in _CHEAP_KINDS:
            return CHEAP
        if kind in _MID_KINDS:
            return MID

    # Non-engineering work, or a forced agent run with no plan: engineering and
    # research default to the best pool, everything else to mid.
    return BEST if domain in ("engineering", "research") else MID


def _needs_best(plan: ExecutionPlan, confidence: float) -> bool:
    """True when the plan's shape alone justifies the best pool, whatever its kind."""
    return plan.execution_path == ExecutionPath.DEEP or plan.mode in _BEST_MODES or confidence < _LOW_CONFIDENCE
