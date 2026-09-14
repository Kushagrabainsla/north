"""Deterministic tests for shared tool-retrieval primitives."""

from tools.retrieval import bm25_rank, reciprocal_rank_fusion, tool_retrieval_profile


class _WeatherTool:
    name = "weather_lookup"
    description = "Get current weather."
    parameters_schema = {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "City to inspect.",
            }
        },
    }


def test_profile_contains_name_purpose_and_argument_semantics() -> None:
    profile = tool_retrieval_profile(_WeatherTool())

    assert "weather lookup (weather_lookup)" in profile
    assert "Get current weather" in profile
    assert "Argument location: City to inspect." in profile


def test_bm25_omits_unmatched_documents() -> None:
    documents = {
        "weather_lookup": "weather forecast city temperature",
        "write_file": "write workspace content",
    }

    assert bm25_rank("city weather", documents) == ["weather_lookup"]
    assert bm25_rank("unrelated phrase", documents) == []


def test_fusion_combines_rankings_and_boosts_explicit_identifiers() -> None:
    assert reciprocal_rank_fusion(
        ["web_search", "read_file"],
        ["web_search", "read_file"],
        exact_query="Use read_file first",
    )[0] == "read_file"
