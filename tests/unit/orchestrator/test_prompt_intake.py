"""A prompt of only spaces is refused where tasks arrive, as an empty one is.

Issue #76, M21: POST /web/api/conversations/<id>/turns {"prompt": "   "} was
accepted (202); the planner and an agent ran (39k input tokens) to say "The task
came through empty" (task_cc94ef2ac3a5).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator.models import TaskRequest
from web.api import TurnCreate


@pytest.mark.parametrize("model", [TaskRequest, TurnCreate])
@pytest.mark.parametrize("prompt", ["   ", "\n\t "])
def test_a_prompt_of_only_spaces_is_refused(model, prompt: str) -> None:
    with pytest.raises(ValidationError, match="a prompt needs words"):
        model(prompt=prompt)


@pytest.mark.parametrize("model", [TaskRequest, TurnCreate])
def test_a_prompt_keeps_its_own_spacing(model) -> None:
    assert model(prompt="  list my files  ").prompt == "  list my files  "
