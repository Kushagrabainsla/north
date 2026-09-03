"""Helpers for the pre-chain pool router.

The pool router shuffles among models it considers equal in quality, because a
price-derived score genuinely could not tell them apart. The chain router does
not need this: it ranks on measured scores and is deliberately deterministic.
This module goes when ``NORTH_ROUTING=legacy`` does.
"""

from __future__ import annotations

import random
from collections.abc import Callable

from inference.capability import ModelInfo
from inference.provider import Provider

# A single routing candidate: model metadata paired with its provider instance.
_Candidate = tuple[ModelInfo, Provider]


def shuffle_groups(
    items: list[_Candidate],
    key: Callable[[_Candidate], object],
) -> list[_Candidate]:
    """Shuffle within consecutive equal-key groups, preserving group order.

    Ensures models with identical effective quality are tried in random order
    across calls, distributing load uniformly across equivalent candidates.
    """
    if not items:
        return items
    result: list[_Candidate] = []
    group: list[_Candidate] = [items[0]]
    group_key = key(items[0])
    for item in items[1:]:
        k = key(item)
        if k != group_key:
            random.shuffle(group)
            result.extend(group)
            group = [item]
            group_key = k
        else:
            group.append(item)
    random.shuffle(group)
    result.extend(group)
    return result
