"""Intent Classifier (Stage 1).

See docs/CODING_STYLE.md Sections 5.3, 6.2, 9.7, 13.
"""

from __future__ import annotations

import json
import re

from inference import CompletionRequest, InferenceRouter, PoolPriority
from orchestrator.exceptions import ClassifierError
from orchestrator.models import IntentClassification
from utils.prompts import load_prompt


class IntentClassifier:
    """Classifies user prompts into domains and identifies consequential tasks."""

    def __init__(self, inference_router: InferenceRouter) -> None:
        self._inference_router = inference_router

    async def classify(self, prompt: str, task_id: str | None = None) -> IntentClassification:
        """Classifies the user prompt.

        Args:
            prompt: The user's input prompt.
            task_id: The optional task ID associated with this request.

        Returns:
            The intent classification.

        Raises:
            ClassifierError: If classification fails or output cannot be parsed.
        """
        try:
            system_prompt = load_prompt("prompts/classifier.md")
        except Exception as e:
            raise ClassifierError(f"Failed to load classifier prompt template: {e}") from e

        full_prompt = f"{system_prompt}\n\nTask: {prompt}"

        try:
            response = await self._inference_router.complete(
                CompletionRequest(
                    prompt=full_prompt,
                    priority=PoolPriority.MEDIUM,
                    component="classifier",
                    task_id=task_id,
                )
            )
        except Exception as e:
            raise ClassifierError(f"Inference call failed during classification: {e}") from e

        text = response.text.strip()
        # Clean markdown code block wraps if present
        if text.startswith("```"):
            # Remove leading ```json or ``` and trailing ```
            text = re.sub(r"^```(?:json)?\n", "", text)
            text = re.sub(r"\n```$", "", text)
            text = text.strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ClassifierError(
                f"Failed to parse classifier output as JSON: {text}. Error: {e}"
            ) from e

        # Ensure all required fields exist
        for field in ("is_consequential", "domain", "reasoning"):
            if field not in data:
                raise ClassifierError(
                    f"Classifier response is missing required field '{field}': {data}"
                )

        return IntentClassification(
            is_consequential=bool(data["is_consequential"]),
            domain=str(data["domain"]),
            reasoning=str(data["reasoning"]),
        )
