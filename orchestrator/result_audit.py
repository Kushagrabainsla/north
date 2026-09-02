"""Auditing an agent's answer before it is accepted.

An agent narrates what it did - "created the file", "the tests pass". The model
has no way of knowing whether that is true; it writes what a successful answer
sounds like. Three checks stand between that narration and the recorded result:

1. **Claims vs evidence.** Completion claims in the answer are cross-checked
   against the tools that actually succeeded (`orchestrator/verification.py`).
2. **The engineering evidence gate.** Code that changed with nothing run to
   check it, or an edit that was attempted and never landed, is a false "done"
   no matter how the answer is worded.
3. **Self-repair.** Before anything is flagged, the agent gets one pass to
   either do the work or drop the claim.

Everything here is non-fatal and fails open: a violation annotates the answer
and is recorded, so a fabricated completion is visible rather than silent. The
opt-in critic pass at the end is the same shape - it flags a gap, never rewrites.
"""

from __future__ import annotations

import asyncio
import logging

from agents.base import Agent
from agents.constants import CODE_MUTATING_TOOLS, CODE_VERIFY_TOOLS, ENGINEERING_AGENTS
from agents.models import AgentPayload, AgentResult
from inference.cost_tracker import CostTracker
from inference.models import CompletionRequest, PoolPriority
from orchestrator.engineering_prompts import CRITIC_PROMPT
from orchestrator.journal import TaskJournal
from orchestrator.verification import verify_claims
from utils.ids import generate_id
from utils.text import extract_json

logger = logging.getLogger(__name__)

# How much of the request and the answer the critic is shown. Enough to judge
# whether the answer addresses the request; short enough to stay a cheap call.
_CRITIC_REQUEST_CHARS = 1500
_CRITIC_ANSWER_CHARS = 3000

_SELF_REPAIR_INSTRUCTION = (
    "Either actually perform those actions using your tools, or rewrite your answer to "
    "remove any claim you did not verify with a tool. Never state something was done unless a tool did it."
)


class ResultAuditor:
    """Checks an agent's answer against the evidence, repairing it where it can."""

    def __init__(
        self,
        *,
        journal: TaskJournal,
        self_repair: bool = True,
        critic: bool = False,
        tracked_router: CostTracker | None = None,
    ) -> None:
        self._journal = journal
        self._self_repair = self_repair
        self._critic = critic
        self._tracked_router = tracked_router

    async def audit(self, task_id: str, agent: Agent, result: AgentResult, payload: AgentPayload | None = None) -> None:
        """Run every check over a finished agent result, annotating it in place."""
        await self.verify_claims(task_id, agent, result, payload)
        await self.critique(task_id, agent, result, payload)

    # ------------------------------------------------------------------ #
    #  Claims vs evidence                                                  #
    # ------------------------------------------------------------------ #

    async def verify_claims(
        self, task_id: str, agent: Agent, result: AgentResult, payload: AgentPayload | None = None
    ) -> None:
        """Flag final-answer claims unsupported by tool evidence, repairing first."""
        # Only agentic agents report tool evidence (successful_tools is a list,
        # possibly empty); questions/approvals carry no completion claims.
        if result.successful_tools is None or result.requires_approval or result.has_question:
            return
        workspace = payload.workspace if payload is not None else None
        # verify_claims stats claimed paths on disk - off-thread so the check
        # never blocks the event loop (CODING_STYLE §10.3).
        violations = await asyncio.to_thread(verify_claims, result.output, result.successful_tools, workspace)
        violations = self._with_evidence_gate_violations(agent, result, violations)
        if not violations:
            return

        if self._self_repair and payload is not None:
            violations = await self._attempt_self_repair(task_id, agent, result, payload, violations)
            if not violations:
                return

        bullets = "\n".join(f"- {v}" for v in violations)
        await self._annotate(
            task_id,
            result,
            "\n\n---\n"
            "⚠️ **Unverified claims** - no tool evidence was found for part of this answer:\n"
            f"{bullets}\n\n"
            "Treat the above as not done until confirmed.",
        )
        await self._journal.record(
            task_id,
            "claims_unverified",
            agent=agent.name,
            output="; ".join(violations),
            error_type="unverified_claims",
            payload={"agent": agent.name, "violations": violations},
        )

    def _with_evidence_gate_violations(self, agent: Agent, result: AgentResult, violations: list[str]) -> list[str]:
        """Add the engineering evidence gate's findings to *violations*.

        Code that changed but was never checked is unverified. Code that was
        *attempted* and did not land (e.g. the approval was denied) means no change
        was applied at all, so a "done" claim is simply false. Either routes the
        answer through self-repair rather than being accepted.
        """
        if agent.name not in ENGINEERING_AGENTS:
            return violations
        succeeded = set(result.successful_tools or [])
        attempted = set(result.tools_used or [])
        if (attempted & CODE_MUTATING_TOOLS) and not (succeeded & CODE_MUTATING_TOOLS):
            return [
                *violations,
                "attempted to modify code but the edit did not succeed (it may have been "
                "denied or failed) - no change was applied",
            ]
        if (succeeded & CODE_MUTATING_TOOLS) and not (succeeded & CODE_VERIFY_TOOLS):
            return [*violations, "modified code but ran no check_types or test to verify the change"]
        return violations

    async def _attempt_self_repair(
        self, task_id: str, agent: Agent, result: AgentResult, payload: AgentPayload, violations: list[str]
    ) -> list[str]:
        """Give *agent* one pass to fix unsupported claims; return remaining violations.

        Adopts the corrected answer only if it has strictly fewer violations,
        folding its cost/tokens into *result*. On any failure the original answer
        and its violations are kept.
        """
        feedback = (
            "A verification check found claims in your previous answer with no tool evidence:\n"
            + "\n".join(f"- {v}" for v in violations)
            + f"\n\n{_SELF_REPAIR_INSTRUCTION}"
        )
        repair_payload = payload.model_copy(
            update={
                "run_id": generate_id(),
                "parent_run_id": result.run_id or payload.run_id,
                "attempt": payload.attempt + 1,
                "prompt": f"{payload.prompt}\n\n[correction required]\n{feedback}",
            }
        )
        await self._journal.emit(task_id, "self_repair_started", {"agent": agent.name, "violations": violations})
        try:
            repaired = await agent.run(repair_payload)
        except Exception:
            logger.warning("self-repair: agent %s failed during correction pass", agent.name, exc_info=True)
            return violations
        if repaired.successful_tools is None:
            return violations

        remaining = await asyncio.to_thread(
            verify_claims, repaired.output, repaired.successful_tools, payload.workspace
        )
        if len(remaining) >= len(violations):
            return violations  # no improvement - keep the original answer

        _adopt_repaired_answer(result, repaired)
        await self._journal.record(
            task_id,
            "self_repair",
            agent=agent.name,
            output=f"corrected {len(violations) - len(remaining)} unverified claim(s)",
            event="self_repair_done",
            payload={"agent": agent.name, "remaining": remaining},
        )
        return remaining

    # ------------------------------------------------------------------ #
    #  The critic pass                                                     #
    # ------------------------------------------------------------------ #

    async def critique(self, task_id: str, agent: Agent, result: AgentResult, payload: AgentPayload | None) -> None:
        """Have a fast reviewer flag an answer that does not address the request.

        Opt-in (settings.critic_enabled). Non-blocking: on a clear gap it appends a
        short reviewer note and records a ``critic_flagged`` entry; it never rewrites
        or retries. Fails open - any error leaves the answer untouched.
        """
        if not self._critic or payload is None or self._tracked_router is None:
            return
        if result.requires_approval or result.has_question or not result.output.strip():
            return
        prompt = CRITIC_PROMPT.format(
            request=payload.prompt[:_CRITIC_REQUEST_CHARS], answer=result.output[:_CRITIC_ANSWER_CHARS]
        )
        try:
            response = await self._tracked_router.complete(
                CompletionRequest(
                    prompt=prompt,
                    priority=PoolPriority.MEDIUM,
                    component="critic",
                    task_id=task_id,
                    json_mode=True,
                )
            )
            verdict = extract_json(response.text)
        except Exception:
            logger.debug("critic: review failed for agent %s", agent.name, exc_info=True)
            return

        if verdict.get("adequate", True):
            return
        gap = str(verdict.get("gap", "")).strip()
        if not gap:
            return
        await self._annotate(task_id, result, f"\n\n> ⚠️ **Reviewer note:** {gap}")
        await self._journal.record(
            task_id,
            "critic_flagged",
            agent=agent.name,
            output=gap,
            payload={"agent": agent.name, "gap": gap},
        )

    async def _annotate(self, task_id: str, result: AgentResult, note: str) -> None:
        """Attach a note to the answer and stream it, so a live reader sees it too."""
        result.output = f"{result.output}{note}"
        await self._journal.stream_note(task_id, note)


def _adopt_repaired_answer(result: AgentResult, repaired: AgentResult) -> None:
    """Replace *result*'s answer with the corrected one, folding in its cost."""
    result.output = repaired.output
    result.summary = repaired.summary
    result.data = repaired.data
    result.successful_tools = repaired.successful_tools
    result.tools_used = repaired.tools_used
    result.cost_usd += repaired.cost_usd
    result.tokens_in += repaired.tokens_in
    result.tokens_out += repaired.tokens_out
