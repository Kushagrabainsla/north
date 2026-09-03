"""Model routing: which model serves which part of a task, and in what order.

``parts.py``         what each part of a task needs and what it should be ranked by
``chain.py``         building the ordered, exhaustive candidate chain and walking it
``availability.py``  whether a given endpoint may be called right now, and why not
``legacy.py``        helpers for the pre-chain pool router, kept until it is removed

The chain is the whole list, best first: the free tier is its tail rather than a
separate fallback, so there is exactly one ordering to keep correct.
"""

from __future__ import annotations

from inference.routing.legacy import _Candidate, shuffle_groups

__all__ = ["_Candidate", "shuffle_groups"]
