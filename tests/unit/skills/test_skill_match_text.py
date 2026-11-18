"""North's own name is not part of what a skill is matched on (issue #82, a small item).

"Create an issue for this in north's repo" was offered adding-a-north-tool and
authoring-a-north-flow: every skill for building north's parts carries "north" in
its name, and the user says "north" whenever they mean their project. Measured with
north's local model (bge-small-en-v1.5): that request fell from 0.731 to 0.650 and
"What is north's version?" from 0.720 to 0.536, below the 0.70 suggestion floor,
while requests that want those skills scored higher.
"""

from __future__ import annotations

from pathlib import Path

from skills.models import Skill
from skills.selector import _retrieval_key


def _skill(name: str, description: str) -> Skill:
    return Skill(name=name, description=description, body="1. Do it.", directory=Path("."))


def test_the_match_text_leaves_out_norths_name_and_keeps_the_rest() -> None:
    key = _retrieval_key(_skill("adding-a-north-tool", "Use when north needs a new capability in North's catalog."))

    assert "north" not in key.lower()
    assert "adding a  tool" in key or "adding a tool" in key
    assert "new capability" in key and "catalog" in key


def test_a_word_that_only_contains_north_is_kept() -> None:
    key = _retrieval_key(_skill("travel-planning", "Use when planning a trip to the northeast or Northampton."))

    assert "northeast" in key and "Northampton" in key
