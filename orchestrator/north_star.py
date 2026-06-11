"""North Star Checker (Stage 2).

See docs/CODING_STYLE.md Sections 5.3, 6.4, 9.7, 13.
"""

from __future__ import annotations

import json

from context import ContextDocument, ContextStore
from inference import CompletionRequest, InferenceRouter, PoolPriority
from orchestrator.exceptions import OrchestratorError
from utils.prompts import load_prompt
from utils.text import strip_code_fences


class NorthStarChecker:
    """Checks tasks against active goals across all time horizons."""

    def __init__(self, context_store: ContextStore, inference_router: InferenceRouter) -> None:
        self._context_store = context_store
        self._inference_router = inference_router

    async def check_alignment(self, prompt: str, task_id: str | None = None) -> tuple[bool, str | None, str]:
        """Evaluates whether the prompt aligns with the goals in `north_stars.md`.

        Args:
            prompt: The user's input prompt.
            task_id: The optional task ID associated with this request.

        Returns:
            A tuple of (aligned, tension, reasoning), where:
                aligned: True if aligned, False if conflict/tension found.
                tension: A string explanation if conflict, otherwise None.
                reasoning: The evaluation reasoning.

        Raises:
            OrchestratorError: If verification/inference fails.
        """
        goals = await self._context_store.read(ContextDocument.NORTH_STARS)
        if not goals.strip():
            return True, None, "No active goals found in north_stars.md. Proceeding."

        try:
            system_prompt = load_prompt("prompts/north_star.md")
        except Exception as e:
            raise OrchestratorError(f"Failed to load North Star prompt template: {e}") from e

        full_prompt = (
            f"{system_prompt}\n\n=== Active Goals (north_stars.md) ===\n{goals}\n\n=== Task Request ===\n{prompt}"
        )

        try:
            response = await self._inference_router.complete(
                CompletionRequest(
                    prompt=full_prompt,
                    priority=PoolPriority.MEDIUM,
                    component="north_star_checker",
                    task_id=task_id,
                    json_mode=True,
                )
            )
        except Exception as e:
            raise OrchestratorError(f"Inference call failed during North Star check: {e}") from e

        text = strip_code_fences(response.text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise OrchestratorError(f"Failed to parse North Star checker output as JSON: {text}. Error: {e}") from e

        for field in ("aligned", "reasoning"):
            if field not in data:
                raise OrchestratorError(f"North Star check response is missing required field '{field}': {data}")

        aligned = bool(data["aligned"])
        tension = data.get("tension")
        reasoning = str(data["reasoning"])

        # Normalize empty/null tension
        if not tension or str(tension).lower() == "null":
            tension = None

        return aligned, tension, reasoning
