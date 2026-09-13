"""Time-sensitive orchestration prompts keep their changing clock in the suffix."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from memory.models import ContextDocument
from orchestrator.north_star import NorthStarChecker


async def test_north_star_gets_runtime_context_beside_the_task() -> None:
    memory = SimpleNamespace(read_document=AsyncMock(return_value="Finish the launch by Sep 20."))
    inference = SimpleNamespace(
        complete=AsyncMock(return_value=SimpleNamespace(text='{"aligned": true, "reasoning": "yes"}'))
    )
    checker = NorthStarChecker(memory, inference)

    await checker.check_alignment("Prepare the launch checklist")

    prompt = inference.complete.call_args.args[0].prompt
    assert "trusted factual metadata" in prompt
    assert "<north_runtime_context>" in prompt
    assert prompt.index("<north_runtime_context>") < prompt.index("=== Task Request ===")


async def test_context_trim_gets_runtime_context_outside_the_document(tmp_path) -> None:
    from memory.extraction import ExtractionPipeline

    existing = "A current goal.\n" * 600
    documents = SimpleNamespace(
        read=AsyncMock(return_value=existing),
        write=AsyncMock(),
    )
    inference = SimpleNamespace(complete=AsyncMock(return_value=SimpleNamespace(text="A current goal.")))
    pipeline = ExtractionPipeline(SimpleNamespace(), documents, inference, tmp_path)

    await pipeline._maybe_trim(ContextDocument.NORTH_STARS, "task-1")

    prompt = inference.complete.call_args.args[0].prompt
    assert "<north_runtime_context>" in prompt
    assert prompt.index("</north_runtime_context>") < prompt.index("Document:\n---")
    assert existing in prompt
