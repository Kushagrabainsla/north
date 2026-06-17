"""ResultSynthesizer - merges outputs from multiple agents into one response.

Invoked by the Orchestrator after Stage 4 when more than one agent contributed
to a task. Uses the cheapest inference pool (LOW / high_volume) because the
task is text merging, not reasoning.

See docs/CODING_STYLE.md Sections 2.1, 6.4, 13.
"""

from __future__ import annotations

import logging

from inference.base import InferenceRouter
from inference.models import CompletionRequest, PoolPriority
from utils.prompts import load_prompt

logger = logging.getLogger(__name__)


class ResultSynthesizer:
    """Merges multiple agent outputs into a single coherent markdown response."""

    def __init__(self, inference_router: InferenceRouter) -> None:
        self._inference_router = inference_router

    async def synthesize(
        self,
        agent_outputs: dict[str, str],
        task_id: str,
    ) -> str | None:
        """Return a merged markdown string, or None if synthesis is not needed.

        Args:
            agent_outputs: Mapping of agent name → markdown output string.
                           Agents with empty output are excluded automatically.
            task_id: Task identifier passed through to the inference router for
                     ledger correlation.

        Returns:
            Synthesized markdown string, or None when fewer than two agents
            produced non-empty output (no merging required).
        """
        non_empty = {agent: output for agent, output in agent_outputs.items() if output and output.strip()}
        if len(non_empty) < 2:
            return None

        sections = "\n\n".join(f"## {agent.capitalize()} Agent\n{output}" for agent, output in non_empty.items())

        try:
            system_prompt = load_prompt("prompts/synthesizer.md")
        except Exception:
            logger.warning("ResultSynthesizer: synthesizer prompt not found; skipping synthesis")
            return None

        full_prompt = f"{system_prompt}\n\n---\n\n{sections}"

        try:
            response = await self._inference_router.complete(
                CompletionRequest(
                    prompt=full_prompt,
                    priority=PoolPriority.LOW,
                    component="synthesizer",
                    task_id=task_id,
                )
            )
        except Exception:
            logger.warning("ResultSynthesizer: inference call failed for task %s; returning None", task_id)
            return None

        return response.text.strip() or None
