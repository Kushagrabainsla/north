"""Shared math utilities."""

from __future__ import annotations


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Return the cosine similarity between two embedding vectors.

    Returns 0.0 when either vector has zero magnitude to avoid division by zero.
    """
    import numpy as np

    va = np.array(a, dtype=float)
    vb = np.array(b, dtype=float)
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))
