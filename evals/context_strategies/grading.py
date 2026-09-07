"""Deterministic scorers. No model grades another model here.

Every task's answer is known by construction, so a score is arithmetic. That is
what makes two strategies comparable: the only thing that varies between two
cells of the matrix is the strategy itself.
"""

from __future__ import annotations

import json
import re

_NUMBER_RE = re.compile(r"-?\d[\d,]*")
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _normalise(text: str) -> str:
    return " ".join(text.lower().split()).strip(" .\"'`*")


def score_exact(answer: object, reply: str) -> float:
    """1.0 when the expected string appears in the reply, else 0.0.

    Containment rather than equality: a model that answers "The code is 7743-QX"
    has found it, and punishing the wrapper would measure instruction-following
    instead of retrieval.
    """
    return 1.0 if _normalise(str(answer)) in _normalise(reply) else 0.0


def score_number(answer: object, reply: str) -> float:
    """1.0 when the last number in the reply matches. Models put it last."""
    found = _NUMBER_RE.findall(reply)
    if not found:
        return 0.0
    try:
        return 1.0 if int(found[-1].replace(",", "")) == int(answer) else 0.0
    except ValueError:
        return 0.0


def _parsed_pairs(reply: str) -> set[frozenset[str]]:
    match = _JSON_BLOCK_RE.search(reply)
    if not match:
        return set()
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return set()
    raw = payload.get("pairs") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return set()
    pairs: set[frozenset[str]] = set()
    for item in raw:
        if isinstance(item, list | tuple) and len(item) == 2:
            pairs.add(frozenset(_normalise(str(name)) for name in item))
    return pairs


def score_pairs_f1(answer: object, reply: str) -> float:
    """F1 over the set of pairs, so partial credit is visible.

    A pairs task scored pass/fail would read as a row of zeros and hide which
    strategy got closer - which is the whole question being asked here.
    """
    expected = {frozenset(_normalise(name) for name in pair) for pair in answer}
    predicted = _parsed_pairs(reply)
    if not expected and not predicted:
        return 1.0
    if not expected or not predicted:
        return 0.0
    overlap = len(expected & predicted)
    if not overlap:
        return 0.0
    precision = overlap / len(predicted)
    recall = overlap / len(expected)
    return 2 * precision * recall / (precision + recall)


SCORERS = {
    "exact": score_exact,
    "number": score_number,
    "pairs_f1": score_pairs_f1,
}


def score(scorer: str, answer: object, reply: str) -> float:
    return SCORERS[scorer](answer, reply)
