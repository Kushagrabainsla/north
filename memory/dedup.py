"""Text normalization used to recognize facts that restate one another."""

from __future__ import annotations

import re

# Words that carry no distinguishing meaning for duplicate detection.
FILLER_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "was",
        "were",
        "am",
        "are",
        "be",
        "been",
        "being",
        "has",
        "have",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "my",
        "your",
        "his",
        "her",
        "their",
        "our",
        "its",
        "this",
        "that",
        "these",
        "those",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "as",
        "or",
        "and",
        "but",
    }
)


def normalize_for_dedup(text: str) -> str:
    """Lowercase, drop punctuation and filler words, and collapse whitespace."""
    lowered = text.lower()
    without_punctuation = re.sub(r"[^\w\s]", " ", lowered)
    collapsed = re.sub(r"\s+", " ", without_punctuation).strip()
    return " ".join(word for word in collapsed.split() if word not in FILLER_WORDS)
