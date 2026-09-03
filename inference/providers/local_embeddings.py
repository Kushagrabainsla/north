"""Local, on-device embeddings - no key, no network, no rate limit, no cost.

Embeddings in north are *index* infrastructure: they power tool selection, the
code index, and memory recall. Every one of those is high volume, latency
sensitive, and entirely about the user's own data - none of it is a good reason
to need an API key or a network round trip.

Making them remote also made them fragile. Of the providers north talks to,
only Gemini sells embeddings at all: OpenRouter's catalog has none, Groq has
none, and Codex refuses outright. So one empty key silently disabled semantic
tool search, code search and memory recall - north kept working, but fell back
to keyword overlap and to injecting every tool into every prompt.

This provider serves embeddings from a static model held in memory. It answers
a batch of 200 texts in about 2 ms, where a remote call costs a round trip. The
model weights are fetched once on first use and cached on disk by the
underlying library; if that fetch fails the provider simply reports no models
and north degrades exactly as it does today.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from inference.capability import ModelCapability, ModelInfo
from inference.exceptions import InferenceError, TranscriptionError
from inference.models import (
    CompletionRequest,
    CompletionResponse,
    EmbedRequest,
    EmbedResponse,
    ToolCallRequest,
    ToolCallResponse,
    TranscriptionRequest,
    TranscriptionResponse,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "local"

# A retrieval-tuned static embedding model: 512 dimensions, ~30 MB on disk, and
# fast enough on CPU that batching stops mattering. Static means there is no
# transformer forward pass at inference - the library needs numpy and a
# tokenizer, not torch or an ONNX runtime.
DEFAULT_MODEL_ID = "minishlab/potion-retrieval-32M"


class LocalEmbeddingProvider:
    """Serves the EMBEDDING capability from a model running in this process."""

    name = PROVIDER_NAME

    def __init__(self, model_id: str = DEFAULT_MODEL_ID) -> None:
        self._model_id = model_id
        self._model: Any | None = None
        self._models: dict[str, ModelInfo] = {}
        # One loader at a time: refresh() and a first embed() can race, and the
        # model is tens of megabytes to materialise.
        self._load_lock = asyncio.Lock()

    @property
    def model_id(self) -> str:
        """The model this provider serves, known before it is loaded.

        Callers need this at startup to stamp their vector stores, which happens
        before the first embedding call - so it must not depend on the weights
        having been materialised yet.
        """
        return self._model_id

    # ---- Provider protocol ----

    def get_models(self) -> dict[str, ModelInfo]:
        return dict(self._models)

    async def refresh(self) -> None:
        """Load the model if it is not loaded. Never raises.

        A failure here means north has no local embeddings - the same state it
        was in before this provider existed - so it is logged and swallowed
        rather than failing the whole pool refresh.
        """
        await self._ensure_loaded()

    async def embed(self, model_id: str, request: EmbedRequest) -> EmbedResponse:
        del model_id  # this provider serves exactly one model
        model = await self._ensure_loaded()
        if model is None:
            raise InferenceError("Local embedding model is unavailable")
        if not request.texts:
            return EmbedResponse(embeddings=[], model_used=self._model_id, cost_usd=0.0)
        # Encoding is CPU-bound and fast, but a large batch would still stall the
        # event loop, so it runs in a worker thread like every other blocking call.
        vectors = await asyncio.to_thread(model.encode, list(request.texts))
        return EmbedResponse(
            embeddings=[[float(value) for value in row] for row in vectors],
            model_used=self._model_id,
            cost_usd=0.0,
        )

    async def complete(self, model_id: str, request: CompletionRequest) -> CompletionResponse:
        del model_id, request
        raise InferenceError("The local provider serves embeddings only")

    async def complete_with_tools(
        self, model_id: str, request: ToolCallRequest, token_callback=None
    ) -> ToolCallResponse:
        del model_id, request, token_callback
        raise InferenceError("The local provider serves embeddings only")

    async def transcribe(self, model_id: str, request: TranscriptionRequest) -> TranscriptionResponse:
        del model_id, request
        raise TranscriptionError("The local provider serves embeddings only")

    # ---- Internals ----

    async def _ensure_loaded(self) -> Any | None:
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is not None:
                return self._model
            model = await asyncio.to_thread(self._load_sync)
            if model is None:
                return None
            self._model = model
            self._models = {
                self._model_id: ModelInfo(
                    model_id=self._model_id,
                    provider_name=PROVIDER_NAME,
                    capabilities=frozenset({ModelCapability.EMBEDDING}),
                    context_window=0,  # not applicable to embeddings
                    cost_per_token=0.0,
                    base_quality=1.0,
                )
            }
            return model

    def _load_sync(self) -> Any | None:
        try:
            from model2vec import StaticModel
        except ImportError:
            logger.info(
                "model2vec is not installed - local embeddings are unavailable "
                "(install north's dependencies to enable them)"
            )
            return None
        try:
            model = StaticModel.from_pretrained(self._model_id)
        except Exception as exc:
            logger.warning(
                "Could not load the local embedding model %s (%s) - "
                "semantic tool search, code search and memory recall will fall back to keywords",
                self._model_id,
                exc,
            )
            return None
        logger.info("Local embeddings ready: %s", self._model_id)
        return model
