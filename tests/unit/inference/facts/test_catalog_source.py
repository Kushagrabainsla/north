"""A provider's own capability declaration is believed before a guess from the model's name.

On 2026-11-22 a fresh north with no chat provider sent completions to its local embedding model,
"BAAI/bge-small-en-v1.5": the name has no "embed" in it, so the name guess made it a chat model,
although the provider declares it embeddings-only. Each call failed, and Home listed the model as
waiting out a limit.
"""

from __future__ import annotations

from inference.capability import ModelCapability, ModelInfo
from inference.facts.sources.catalog import endpoints_from_models, inferred_facts
from inference.providers.local_embeddings import DEFAULT_MODEL_ID, PROVIDER_NAME
from inference.routing.chain import Requirements, meets


def _info(model_id: str, capabilities: frozenset[ModelCapability], provider: str = "test") -> ModelInfo:
    return ModelInfo(
        model_id=model_id,
        provider_name=provider,
        capabilities=capabilities,
        context_window=8192,
        cost_per_token=0.0,
        base_quality=0.5,
    )


def test_the_local_embedding_model_is_never_a_completion_candidate() -> None:
    models = {DEFAULT_MODEL_ID: _info(DEFAULT_MODEL_ID, frozenset({ModelCapability.EMBEDDING}), PROVIDER_NAME)}

    [facts] = inferred_facts(models)

    assert facts.value("supports_completion") is False
    assert meets(facts, endpoints_from_models(models), Requirements()) == "not a completion model"


def test_a_chat_model_its_provider_declares_stays_a_candidate() -> None:
    caps = frozenset({ModelCapability.COMPLETION, ModelCapability.TOOL_CALLS})
    models = {"acme-chat-1": _info("acme-chat-1", caps)}

    [facts] = inferred_facts(models)

    assert facts.value("supports_completion") is True
    assert meets(facts, endpoints_from_models(models), Requirements()) is None


def test_without_a_declaration_the_name_is_still_the_guess() -> None:
    [facts] = inferred_facts({"text-embed-3": _info("text-embed-3", frozenset())})

    assert facts.value("supports_completion") is False
