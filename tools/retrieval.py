"""Shared tool-retrieval profiles and lexical ranking primitives."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable
from typing import Protocol


class ToolMetadata(Protocol):
    name: str
    description: str
    parameters_schema: dict


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tool_retrieval_profile(tool: ToolMetadata) -> str:
    """Describe identity, purpose, and argument semantics for retrieval only."""
    fields = [
        f"Tool name: {tool.name.replace('_', ' ')} ({tool.name}).",
        tool.description,
    ]
    parameters = getattr(tool, "parameters_schema", {})
    properties = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
    if isinstance(properties, dict):
        for name, value in properties.items():
            if not isinstance(value, dict):
                continue
            detail = str(value.get("description", ""))
            choices = value.get("enum")
            if choices:
                detail = f"{detail} Choices: {', '.join(map(str, choices))}."
            fields.append(f"Argument {name.replace('_', ' ')}: {detail}".strip())
    return "\n".join(fields)


def tool_index_documents(tools: Iterable[ToolMetadata]) -> list[tuple[str, str]]:
    """Build the one canonical set of profiles consumed by every tool index."""
    return [(tool.name, tool_retrieval_profile(tool)) for tool in tools]


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower().replace("_", " "))


def bm25_rank(query: str, documents: dict[str, str]) -> list[str]:
    """Rank matching profiles with dependency-free BM25; omit zero-score rows."""
    tokenized = {name: _tokens(text) for name, text in documents.items()}
    query_tokens = _tokens(query)
    if not query_tokens or not tokenized:
        return []
    document_frequency = Counter(
        token for tokens in tokenized.values() for token in set(tokens)
    )
    count = len(tokenized)
    average_length = sum(map(len, tokenized.values())) / count
    scored: list[tuple[float, str]] = []
    for name, tokens in tokenized.items():
        frequencies = Counter(tokens)
        score = 0.0
        for token in query_tokens:
            frequency = frequencies[token]
            if not frequency:
                continue
            inverse_frequency = math.log(
                1
                + (count - document_frequency[token] + 0.5)
                / (document_frequency[token] + 0.5)
            )
            denominator = frequency + 1.5 * (
                1 - 0.75 + 0.75 * len(tokens) / max(average_length, 1)
            )
            score += inverse_frequency * frequency * 2.5 / denominator
        if score > 0:
            scored.append((score, name))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [name for _, name in scored]


def reciprocal_rank_fusion(*rankings: list[str], exact_query: str = "") -> list[str]:
    """Fuse independent rankings and hard-boost explicitly named tools."""
    scores: Counter[str] = Counter()
    for ranking in rankings:
        for index, name in enumerate(ranking, start=1):
            scores[name] += 1 / (60 + index)
    query_identifiers = set(re.findall(r"[a-z][a-z0-9_]+", exact_query.lower()))
    for name in query_identifiers & set(scores):
        scores[name] += 1.0
    return [
        name
        for name, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    ]
