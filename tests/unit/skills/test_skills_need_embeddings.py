"""Whether embeddings work is found by trying them, and skill learning says so before it asks you.

In the issue #76 test the local embedding model could not load (HuggingFace
answered 403 Forbidden, every three minutes in north.log) while Settings read
"Local embeddings ... Ready". Every skill north tried to learn then failed its
selection test after you had approved it: four cards for a status report, seven
for a GitHub digest, four for a reminder (M6).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from approval.store import ApprovalStore
from config.approval_mode import ApprovalMode
from config.strategy import NorthSettings
from inference.dispatcher import ModelDispatcher
from inference.models import EmbedRequest
from inference.providers import local_embeddings
from inference.providers.local_embeddings import LocalEmbeddingProvider
from orchestrator.api_context import ApiServices, bind_services
from skills.registry import SkillRegistry
from skills.selector import SkillSelector
from tests.conftest import bind_approvals
from tools.models import ToolInput
from tools.universal.create_skill import CreateSkillTool


class _Forbidden:
    def __init__(self, model_id: str) -> None:
        raise RuntimeError("403 Forbidden")


@pytest.fixture
def unloadable(monkeypatch: pytest.MonkeyPatch) -> LocalEmbeddingProvider:
    import inference.providers.onnx_encoder as onnx_encoder

    monkeypatch.setattr(onnx_encoder, "OnnxTextEncoder", _Forbidden)
    return LocalEmbeddingProvider()


async def test_a_model_that_cannot_load_is_reported_unavailable_with_its_reason(
    unloadable: LocalEmbeddingProvider, tmp_path: Path
) -> None:
    router = ModelDispatcher([unloadable], cooldowns_path=tmp_path / "cooldowns.json")
    await router.refresh_catalog()

    state, why = router.embedding_state()

    assert state == "unavailable" and "403 Forbidden" in why and local_embeddings.DEFAULT_MODEL_ID in why
    with pytest.raises(Exception, match="No embedding model is available: could not load"):
        await router.embed(EmbedRequest(texts=["hello"], component="embed"))


async def test_the_settings_row_says_the_embedder_is_not_working(
    unloadable: LocalEmbeddingProvider, tmp_path: Path
) -> None:
    from web.api import system_overview

    router = ModelDispatcher([unloadable], cooldowns_path=tmp_path / "cooldowns.json")
    await router.refresh_catalog()
    services = ApiServices(
        north_settings=NorthSettings(tmp_path / "settings.json"), north_home=tmp_path, inference_router=router
    )
    with bind_services(services):
        overview = await system_overview()

    [local] = [row for row in overview["providers"] if row["id"] == "local"]
    assert local["state"] == "unavailable" and "403 Forbidden" in local["state_detail"]
    assert overview["embeddings"]["state"] == "unavailable"


async def test_no_card_is_raised_for_a_skill_that_cannot_be_tested(tmp_path: Path) -> None:
    async def no_embeddings(texts: list[str]) -> list[list[float]]:
        raise RuntimeError("No embedding model is available: could not load BAAI/bge-small-en-v1.5 (403 Forbidden)")

    registry = SkillRegistry(builtin_dir=tmp_path / "builtin", learned_dir=tmp_path / "learned")
    store = ApprovalStore()
    tool = bind_approvals(
        CreateSkillTool(
            registry,
            learned_dir=tmp_path / "learned",
            skill_selector=SkillSelector(registry, embed_fn=no_embeddings),
        ),
        ApprovalMode.ASK,
        store=store,
    )

    # Bounded: on the old code a card went up here and waited for an answer that never came.
    out = await asyncio.wait_for(
        tool.execute(
            ToolInput(
                params={
                    "action": "create",
                    "name": "weekly-status",
                    "description": "Use when writing my weekly status report.",
                    "instructions": "Three bullets: wins, blockers, next.",
                    "positive_prompts": ["write my weekly status report"],
                    "negative_prompts": ["review this python function"],
                }
            )
        ),
        timeout=5,
    )

    assert not out.success
    assert "Skills cannot be learned right now" in out.error and "403 Forbidden" in out.error
    assert store.all() == [], "no card for a change that cannot pass its test"
    assert not (tmp_path / "learned" / "weekly-status").exists()
