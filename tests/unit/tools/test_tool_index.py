"""Tests for hybrid tool selection and its safe fallback behavior."""

from __future__ import annotations

import pytest

from tools.tool_index import SEMANTIC_TOP_K, ToolIndex


class _EmbeddingStub:
    def __init__(self) -> None:
        self.fail_queries = False

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        if self.fail_queries and len(texts) == 1:
            raise RuntimeError("embedding unavailable")
        return [
            [
                float("camera" in text.lower()),
                float("schedule" in text.lower()),
            ]
            for text in texts
        ]


@pytest.mark.asyncio
async def test_hybrid_ranking_recovers_a_strong_lexical_match(tmp_path) -> None:
    embedder = _EmbeddingStub()
    index = ToolIndex(tmp_path / "tools.db", embedder)
    await index.update_tools(
        [
            ("take_photo", "camera image capture"),
            ("schedule_task", "schedule a weekday reminder"),
        ]
    )

    # Dense sees no known vocabulary in "weekday reminder" and ties; BM25
    # supplies the discriminating signal.
    assert await index.search_tools("weekday reminder", top_k=1) == ["schedule_task"]


@pytest.mark.asyncio
async def test_lexical_ranking_survives_query_embedding_failure(tmp_path) -> None:
    embedder = _EmbeddingStub()
    index = ToolIndex(tmp_path / "tools.db", embedder)
    await index.update_tools(
        [
            ("take_photo", "camera image capture"),
            ("schedule_task", "schedule a weekday reminder"),
        ]
    )
    embedder.fail_queries = True

    assert await index.search_tools("weekday reminder", top_k=1) == ["schedule_task"]
    assert await index.search_tools("unmatched vocabulary", top_k=1) == []


def test_benchmarked_default_candidate_count() -> None:
    assert SEMANTIC_TOP_K == 8
