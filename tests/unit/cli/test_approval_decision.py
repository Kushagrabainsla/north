"""Turning a pick on an approval card into a decision the server accepts.

Every gated tool words its own affirmative differently - git offers "Approve",
patch_file "Apply", bash and shell "Run". Reading the label as consent only for
the word "approve" meant `--yolo` approved the branch checkout and then returned
"answered" for the edit itself. "answered" is not consent, so the patch never
applied and the run looked as though it had simply done nothing.
"""

from __future__ import annotations

import pytest

from cli.main import _approval_decision


@pytest.mark.parametrize("chosen", ["Approve", "approved", "Yes", "Apply", "Run", "OK"])
def test_every_tools_affirmative_reads_as_consent(chosen: str) -> None:
    assert _approval_decision(chosen, yolo=False) == "approved"


@pytest.mark.parametrize("chosen", ["Reject", "No", "Cancel", "deny"])
def test_every_tools_refusal_reads_as_a_rejection(chosen: str) -> None:
    assert _approval_decision(chosen, yolo=False) == "rejected"


@pytest.mark.parametrize("chosen", ["Approve", "Apply", "Run", "something unexpected", ""])
def test_yolo_approves_whatever_the_option_is_called(chosen: str) -> None:
    """--yolo means approve everything, and this event only carries approval
    cards - so the first option is a yes regardless of its wording."""
    assert _approval_decision(chosen, yolo=True) == "approved"


def test_an_unrecognised_answer_is_not_guessed_into_consent(chosen: str = "maybe later") -> None:
    """Free text on an approval card is an answer, not a yes. Defaulting it to
    approved would let a typo authorise a write."""
    assert _approval_decision(chosen, yolo=False) == "answered"


def test_surrounding_whitespace_and_case_do_not_change_the_meaning() -> None:
    assert _approval_decision("  APPLY  ", yolo=False) == "approved"
    assert _approval_decision("  Cancel\n", yolo=False) == "rejected"
