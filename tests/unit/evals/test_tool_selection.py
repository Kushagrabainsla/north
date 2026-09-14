"""Deterministic checks for the tool-selection experiment harness."""

from __future__ import annotations

from experiments.tool_selection.retrieval_benchmark import (
    ToolDoc,
    _adaptive_k,
    bm25_rank,
    discover_tool_docs,
    load_cases,
    reciprocal_rank_fusion,
)


def test_cases_only_reference_discovered_tools() -> None:
    docs = discover_tool_docs()
    expected = {name for case in load_cases() for name in case.expected}
    assert expected <= set(docs)


def test_enriched_profile_contains_argument_semantics() -> None:
    doc = ToolDoc(
        name="weather_lookup",
        description="Get weather.",
        parameters_schema={
            "type": "object",
            "properties": {"location": {"type": "string", "description": "City to inspect."}},
        },
    )
    assert "weather lookup" in doc.enriched_profile
    assert "Argument location: City to inspect." in doc.enriched_profile


def test_bm25_uses_tool_names_and_argument_descriptions() -> None:
    documents = {
        "weather_lookup": "Tool name: weather lookup. Argument location: City to inspect.",
        "write_file": "Write content to a workspace file.",
    }
    assert bm25_rank("inspect city weather", documents)[0] == "weather_lookup"


def test_fusion_hard_boosts_an_explicit_tool_identifier() -> None:
    result = reciprocal_rank_fusion(
        ["web_search", "read_file"],
        ["web_search", "read_file"],
        exact_query="Use read_file first",
    )
    assert result[0] == "read_file"


def test_adaptive_k_uses_prompt_language_without_benchmark_labels() -> None:
    assert _adaptive_k("Read the file") == 5
    assert _adaptive_k("Find the file and then patch it") == 8
