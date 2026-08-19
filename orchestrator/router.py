"""Execution Planner / Router (Stage 3).

See docs/CODING_STYLE.md Sections 5.3, 6.5, 9.7, 13.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from config.strategy import NorthSettings

from agents import AgentRegistry
from inference import CompletionRequest, InferenceRouter, PoolPriority
from orchestrator.exceptions import RoutingError
from orchestrator.models import ExecutionMode, ExecutionPath, ExecutionPlan, IntentClassification
from utils.prompts import load_prompt
from utils.text import extract_json

_PLAN_CACHE_TTL_SECONDS: int = 3600  # 1 hour
_PLAN_CACHE_MAX_SIZE: int = 256
_NORMALIZE_RE = re.compile(r"[^a-z0-9 ]")
# Only the most recent dialogue disambiguates a follow-up; cap it so the planner
# prompt and the routing cache key stay bounded on long conversations.
_PLANNER_CONVERSATION_TAIL_CHARS: int = 2000


def _normalize(text: str) -> str:
    return " ".join(_NORMALIZE_RE.sub("", text.lower().strip()).split())


# Deterministic engineering pipeline (#4). The STRUCTURE - which agents run and in
# what order - is fixed by code, not invented by the LLM per call. The canonical
# order never changes; a task only selects a subset of it via `engineering_kind`.
_ENGINEERING_ORDER: tuple[str, ...] = ("researcher", "architect", "coder", "reviewer")
_ENGINEERING_STAGES: dict[str, tuple[str, ...]] = {
    "question": ("researcher",),
    "research": ("researcher", "architect"),
    "bugfix": ("coder", "reviewer"),
    "debug": ("coder", "reviewer"),
    "test": ("coder", "reviewer"),
    "refactor": ("architect", "coder", "reviewer"),
    "feature": ("researcher", "architect", "coder", "reviewer"),
}
# Below this planner confidence, run the full chain rather than trust a subset.
_ENGINEERING_FULL_CHAIN_BELOW_CONFIDENCE: float = 0.6
# Read-only engineering kinds. These must NEVER be escalated into a code-writing
# task, even at low confidence: a vague "how does X work?" stays an investigation
# and must not silently add the coder (which would turn it into a write task).
_NO_CODE_KINDS: frozenset[str] = frozenset({"question", "research"})
# Shipping kinds: take already-completed work and ship it (branch/commit/push/PR/CI).
# Handled by the orchestrator's single-agent, human-gated deploy flow - not the
# researcher→architect→coder→reviewer pipeline - so they map to a coder-only plan.
_DEPLOY_KINDS: frozenset[str] = frozenset({"deploy", "ship"})


def _plan_cache_key(prompt: str, conversation: str = "") -> str:
    """Stable hash of the normalized prompt (plus recent conversation) for routing.

    The conversation is folded in because it now affects routing - the same
    prompt ("yes, go ahead") must not reuse a plan cached under a different
    conversation, or follow-ups would route to the wrong agent.
    """
    normalized = _normalize(prompt)
    if conversation:
        normalized = f"{normalized}␟{_normalize(conversation)}"
    return hashlib.md5(normalized.encode()).hexdigest()


def _recent_conversation(context: str) -> str:
    """Extract just the recent-conversation section from a context blob.

    The planner only needs dialogue to disambiguate follow-ups like "go ahead";
    personal background facts add noise and would pollute the routing cache key.
    Returns "" when the blob has no conversation section (backward compatible).
    """
    if context.startswith("## Recent conversation"):
        # Keep up to the next top-level (##) section, matching how the agent
        # splits the same blob in _build_task_message.
        parts = re.split(r"\n\n(?=##)", context, maxsplit=1)
        section = parts[0].strip().removeprefix("## Recent conversation").strip()
        return section[-_PLANNER_CONVERSATION_TAIL_CHARS:]
    return ""


logger = logging.getLogger(__name__)

# Planning gates the whole task, so a transient inference failure must not silently
# degrade it to a no-op general run. Retry a few times, then fail loudly. Each
# attempt is time-bounded so a hung model can't stall the task forever.
_PLANNER_MAX_ATTEMPTS: int = 3
_PLANNER_RETRY_DELAY_S: float = 2.0
_PLANNER_ATTEMPT_TIMEOUT_S: float = 45.0

if TYPE_CHECKING:
    from tools.registry import ToolRegistry


# Keys a well-formed planner object can carry. Used to recognize a plan dict
# that was accidentally wrapped in a JSON list by the model.
_PLAN_OBJECT_KEYS = frozenset(
    {
        "agents",
        "mode",
        "domain",
        "direct_tool",
        "direct_tool_params",
        "dependencies",
        "parallel_groups",
        "confidence",
        "reasoning",
        "is_consequential",
        "engineering_kind",
    }
)


def _normalize_plan_json(parsed: Any) -> dict[str, Any]:
    """Coerce the planner LLM JSON into the dict shape the parser expects.

    The model is asked for a single JSON object, but it sometimes emits a JSON
    *list* instead (e.g. ``[{"agents": [...], ...}]`` or a bare list of agent
    names). Earlier code assumed a dict and called ``.get()`` on the result, so a
    list response blew up with ``'list' object has no attribute 'get'`` and the
    whole task failed after retries. We unwrap the common list shapes here so the
    planner degrades to the fallback plan instead of crashing, and raise a clear
    RoutingError only for genuinely unusable responses.
    """
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        # Common case: a single-element list wrapping the object we wanted.
        if len(parsed) == 1 and isinstance(parsed[0], dict):
            return parsed[0]
        # A list of object candidates - take the first that looks like a plan.
        for item in parsed:
            if isinstance(item, dict) and _PLAN_OBJECT_KEYS & item.keys():
                return item
        # A bare list of agent-name strings -> treat as the agent list.
        if parsed and all(isinstance(item, str) for item in parsed):
            return {"agents": list(parsed)}
        raise RoutingError(
            "planner returned a JSON list with no plan object: "
            f"{parsed[:3]!r}{'...' if len(parsed) > 3 else ''}"
        )
    # Scalars / None are not a usable plan.
    raise RoutingError(f"planner returned a non-object JSON value: {parsed!r}")


class ExecutionPlanner:
    """Stage 3 orchestrator module that constructs the Agent ExecutionPlan."""

    def __init__(
        self,
        agent_registry: AgentRegistry,
        inference_router: InferenceRouter,
        tool_registry: ToolRegistry | None = None,
        workspace: str = "",
        north_settings: NorthSettings | None = None,
    ) -> None:
        self._agent_registry = agent_registry
        self._inference_router = inference_router
        self._tool_registry = tool_registry
        self._workspace = workspace
        self._north_settings = north_settings
        # Cache: normalized_hash → (insert_ts, classification, plan)
        self._plan_cache: dict[str, tuple[float, IntentClassification, ExecutionPlan]] = {}

    async def plan_all(
        self, prompt: str, task_id: str, context: str = ""
    ) -> tuple[IntentClassification, ExecutionPlan]:
        """Single LLM call that classifies the task AND builds the execution plan.

        Replaces the separate classify → route two-call pipeline. *context* is the
        task's context blob; its recent-conversation section is given to the
        planner so follow-ups ("yes, go ahead") route from the dialogue, not blind.
        """
        all_agents = self._agent_registry.all()
        if not all_agents:
            raise RoutingError("No agents are registered.")

        conversation = _recent_conversation(context)
        cache_key = _plan_cache_key(prompt, conversation)
        cached = self._plan_cache.get(cache_key)
        if cached is not None:
            insert_ts, cached_cls, cached_plan = cached
            # Revalidate against the current registries: agents/tools can be
            # created or removed at runtime, and executing a stale plan fails
            # only at agent-lookup time, well after planning.
            if (time.monotonic() - insert_ts) < _PLAN_CACHE_TTL_SECONDS and self._plan_still_valid(cached_plan):
                logger.debug("Planner cache hit for key %s", cache_key[:8])
                # Return a fresh plan with the new task_id so task tracking is correct.
                return cached_cls, cached_plan.with_task_id(task_id)
            del self._plan_cache[cache_key]

        agents_info = [{"name": a.name, "domain": a.domain, "accepts": a.config.accepts} for a in all_agents]
        tools_info = self._summarise_tools()

        try:
            system_prompt = load_prompt("prompts/planner.md")
        except Exception as e:
            raise RoutingError(f"Failed to load planner prompt: {e}") from e

        system_context_lines = []
        if self._workspace:
            system_context_lines.append(f"- workspace (default cwd for shell/file tools): {self._workspace}")
            system_context_lines.append(
                "- When constructing filesystem paths, always prefer absolute paths derived from the "
                "workspace above. Never emit bare filenames or paths starting with '~' - expand them."
            )
        system_context_block = (
            "=== System Context ===\n" + "\n".join(system_context_lines) + "\n\n" if system_context_lines else ""
        )

        conversation_block = (
            f"=== Recent Conversation ===\n{conversation}\n"
            "(Use this to resolve what the User Task refers to - e.g. a short "
            "confirmation like 'yes, go ahead' continues the work just discussed. "
            "Classify and route based on that actual intent.)\n\n"
            if conversation
            else ""
        )

        full_prompt = (
            f"{system_prompt}\n\n"
            f"{system_context_block}"
            f"=== Available Agents ===\n{json.dumps(agents_info, indent=2)}\n\n"
            f"=== Available Tools ===\n{json.dumps(tools_info, indent=2)}\n\n"
            f"{conversation_block}"
            f"=== User Task ===\n{prompt}"
        )

        # Planning gates the whole task: a transient inference failure is retried
        # inside _complete_plan, and a hard failure raises RoutingError so the task
        # fails honestly instead of silently running a no-op general fallback.
        data = await self._complete_plan(full_prompt, task_id)

        raw_confidence = data.get("confidence", 0.9)
        try:
            confidence = max(0.0, min(1.0, float(raw_confidence)))
        except (TypeError, ValueError):
            confidence = 0.9
        classification = IntentClassification(
            is_consequential=bool(data.get("is_consequential", False)),
            domain=str(data.get("domain", "general")),
            reasoning=str(data.get("reasoning", "")),
            confidence=confidence,
        )
        if classification.domain == "engineering":
            # Deterministic engineering chain (#4): ignore any LLM-invented agent
            # graph and build a fixed researcher→architect→coder→reviewer subset.
            engineering_kind = str(data.get("engineering_kind", "")).strip().lower()
            plan = self._build_engineering_plan(engineering_kind, confidence, task_id)
        else:
            plan = self._build_plan_from_response(data, classification.domain, task_id)

        path = (
            ExecutionPath.DEEP
            if (len(plan.agents) > 1 or plan.mode in (ExecutionMode.PARALLEL, ExecutionMode.HIERARCHICAL))
            else ExecutionPath.FAST
        )
        classification = classification.model_copy(update={"execution_path": path})
        plan = plan.model_copy(update={"execution_path": path})

        # Evict oldest entries when cache is full, then store.
        if len(self._plan_cache) >= _PLAN_CACHE_MAX_SIZE:
            oldest = min(self._plan_cache, key=lambda k: self._plan_cache[k][0])
            del self._plan_cache[oldest]
        self._plan_cache[cache_key] = (time.monotonic(), classification, plan)

        return classification, plan

    # ------------------------------------------------------------------

    def _plan_still_valid(self, plan: ExecutionPlan) -> bool:
        """Return True when every agent/tool the cached plan references still exists."""
        if plan.mode == ExecutionMode.SINGLE_TOOL:
            if self._tool_registry is None or not plan.direct_tool:
                return False
            try:
                self._tool_registry.get(plan.direct_tool)
            except Exception:
                return False
            return True
        registered = set(self._agent_registry.names())
        return bool(plan.agents) and set(plan.agents) <= registered

    def _summarise_tools(self) -> list[dict[str, Any]]:
        """Return a compact list of available tools for the router prompt."""
        if self._tool_registry is None:
            return []
        summaries = []
        for name in sorted(self._tool_registry.all_tool_names()):
            try:
                tool = self._tool_registry.get(name)
                schema = tool.parameters_schema
                required = schema.get("required", [])
                props = schema.get("properties", {})
                params = []
                for param_name, param_def in props.items():
                    req = "required" if param_name in required else "optional"
                    desc = param_def.get("description", "")
                    params.append(f"{param_name} ({req}): {desc}")
                summaries.append({"name": name, "description": tool.description, "params": params})
            except Exception:
                continue
        return summaries

    def _build_plan_from_response(self, data: dict[str, Any], domain: str, task_id: str) -> ExecutionPlan:
        """Parse the router LLM response into an ExecutionPlan."""
        raw_mode = data.get("mode", "single_agent")
        try:
            mode = ExecutionMode(raw_mode)
        except ValueError:
            mode = ExecutionMode.SINGLE_AGENT

        # single_tool path
        if mode == ExecutionMode.SINGLE_TOOL:
            direct_tool = data.get("direct_tool")
            direct_tool_params = data.get("direct_tool_params") or {}
            if not direct_tool or not isinstance(direct_tool_params, dict):
                return self.build_fallback_plan(domain, task_id)
            # Verify the tool actually exists
            if self._tool_registry is not None:
                try:
                    self._tool_registry.get(direct_tool)
                except Exception:
                    return self.build_fallback_plan(domain, task_id)
            return ExecutionPlan(
                task_id=task_id,
                agents=[],
                parallel_groups=[],
                dependencies={},
                mode=ExecutionMode.SINGLE_TOOL,
                direct_tool=direct_tool,
                direct_tool_params=direct_tool_params,
            )

        # agent-based paths
        raw_agents = data.get("agents")
        if not isinstance(raw_agents, list) or not raw_agents:
            return self.build_fallback_plan(domain, task_id)

        registered = set(self._agent_registry.names())
        agents = [a for a in raw_agents if a in registered]
        if not agents:
            return self.build_fallback_plan(domain, task_id)

        raw_deps = data.get("dependencies")
        dependencies: dict[str, list[str]] = {}
        if isinstance(raw_deps, dict):
            for k, v in raw_deps.items():
                if k in agents and isinstance(v, list):
                    dependencies[k] = [dep for dep in v if dep in agents and dep != k]

        parallel_groups = data.get("parallel_groups")
        if not self._is_valid_parallel_groups(parallel_groups, agents):
            try:
                parallel_groups = self._compute_parallel_groups(agents, dependencies)
            except RoutingError:
                logger.warning(
                    "Dependency cycle in LLM response for agents %s - falling back to single agent",
                    agents,
                )
                return self.build_fallback_plan(domain, task_id)

        return ExecutionPlan(
            task_id=task_id,
            agents=agents,
            parallel_groups=parallel_groups,
            dependencies=dependencies,
            mode=mode,
        )

    def _build_engineering_plan(self, engineering_kind: str, confidence: float, task_id: str) -> ExecutionPlan:
        """Deterministic researcher→architect→coder→reviewer pipeline (#4).

        Code - not the LLM - fixes the structure. The canonical order is constant;
        ``engineering_kind`` selects a subset of it. Invariants: the reviewer always
        follows the coder (verification is non-negotiable); a low-confidence *code*
        classification runs the full chain; and a read-only kind (question/research)
        is NEVER escalated into a write task by adding the coder, even at low
        confidence. Agents can still delegate_task to a skipped stage mid-run, so an
        under-selected subset self-corrects.
        """
        registered = {a.name for a in self._agent_registry.all()}
        # Deploy/ship: a single git/gh-capable agent, human-gated by the orchestrator's
        # deploy flow. No reviewer, no full-chain escalation - there is no new code to
        # review here, only work to ship.
        if engineering_kind in _DEPLOY_KINDS:
            if "coder" not in registered:
                return self.build_fallback_plan("engineering", task_id)
            return ExecutionPlan(
                task_id=task_id,
                agents=["coder"],
                parallel_groups=[["coder"]],
                dependencies={},
                mode=ExecutionMode.SINGLE_AGENT,
                engineering_kind=engineering_kind,
            )
        selected: tuple[str, ...] = _ENGINEERING_STAGES.get(engineering_kind, _ENGINEERING_ORDER)
        # Low confidence broadens a code task to the full chain, but a read-only kind
        # must stay read-only - never turn "how does X work?" into a code edit.
        if confidence < _ENGINEERING_FULL_CHAIN_BELOW_CONFIDENCE and engineering_kind not in _NO_CODE_KINDS:
            selected = _ENGINEERING_ORDER
        if "coder" in selected and "reviewer" not in selected:
            selected = (*selected, "reviewer")

        ordered = [name for name in _ENGINEERING_ORDER if name in selected and name in registered]
        if not ordered:
            return self.build_fallback_plan("engineering", task_id)

        # Linear dependencies along the canonical order: each stage waits for the
        # previous selected one, so HIERARCHICAL runs them strictly in sequence.
        dependencies: dict[str, list[str]] = {ordered[i]: [ordered[i - 1]] for i in range(1, len(ordered))}
        mode = ExecutionMode.SINGLE_AGENT if len(ordered) == 1 else ExecutionMode.HIERARCHICAL
        groups = self._compute_parallel_groups(ordered, dependencies)
        return ExecutionPlan(
            task_id=task_id,
            agents=ordered,
            parallel_groups=groups,
            dependencies=dependencies,
            mode=mode,
            engineering_kind=engineering_kind,
        )

    async def _complete_plan(self, full_prompt: str, task_id: str) -> dict[str, Any]:
        """Call the planner LLM and parse its JSON, retrying transient failures.

        Planning gates the whole task, so a transient inference failure (e.g. all
        models briefly rate-limited) or a malformed response must not silently
        degrade the task to a no-op general run. We retry a few times; if every
        attempt fails we raise RoutingError so the task is marked FAILED and can be
        retried - never reported as a false "completed".
        """
        last_exc: Exception | None = None
        max_attempts = (
            self._north_settings.planner_max_attempts
            if self._north_settings is not None
            else _PLANNER_MAX_ATTEMPTS
        )
        base_delay = (
            self._north_settings.planner_retry_delay_seconds
            if self._north_settings is not None
            else _PLANNER_RETRY_DELAY_S
        )
        backoff_factor = (
            self._north_settings.planner_retry_backoff_factor
            if self._north_settings is not None
            else 1.5
        )

        for attempt in range(1, max_attempts + 1):
            try:
                response = await asyncio.wait_for(
                    self._inference_router.complete(
                        CompletionRequest(
                            prompt=full_prompt,
                            priority=PoolPriority.HIGH,
                            component="planner",
                            task_id=task_id,
                            json_mode=True,
                            temperature=0.0,
                        )
                    ),
                    timeout=_PLANNER_ATTEMPT_TIMEOUT_S,
                )
                parsed = extract_json(response.text)
                return _normalize_plan_json(parsed)
            except Exception as exc:
                last_exc = exc
                logger.warning("Planner attempt %d/%d failed: %s", attempt, max_attempts, exc)
                if attempt < max_attempts:
                    sleep_s = base_delay * (backoff_factor ** (attempt - 1))
                    await asyncio.sleep(sleep_s)
        raise RoutingError(f"planner failed after {max_attempts} attempts: {last_exc}")

    def build_fallback_plan(self, domain: str, task_id: str) -> ExecutionPlan:
        """Simple fallback: single agent matching the classified domain."""
        matching = self._agent_registry.for_domain(domain)
        if not matching:
            matching = self._agent_registry.for_domain("general") or [self._agent_registry.all()[0]]
        name = matching[0].name
        return ExecutionPlan(
            task_id=task_id,
            agents=[name],
            parallel_groups=[[name]],
            dependencies={},
            mode=ExecutionMode.SINGLE_AGENT,
        )

    @staticmethod
    def _is_valid_parallel_groups(groups: Any, agents: list[str]) -> bool:
        if not isinstance(groups, list) or not groups:
            return False
        flat: list[str] = []
        for g in groups:
            if not isinstance(g, list):
                return False
            for a in g:
                if not isinstance(a, str):
                    return False
                flat.append(a)
        return set(flat) == set(agents)

    @staticmethod
    def _compute_parallel_groups(agents: list[str], dependencies: dict[str, list[str]]) -> list[list[str]]:
        """Layer-based topological sort to compute parallel execution groups.

        Raises RoutingError if a dependency cycle is detected so callers can
        fall back to a safe single-agent plan rather than silently running
        dependent agents in parallel.
        """
        remaining = set(agents)
        deps = {a: set(dependencies.get(a, [])) for a in agents}
        groups: list[list[str]] = []
        while remaining:
            layer = [a for a in sorted(remaining) if not (deps[a] & remaining)]
            if not layer:
                raise RoutingError(f"Dependency cycle detected among agents: {sorted(remaining)}")
            groups.append(layer)
            remaining -= set(layer)
        return groups
