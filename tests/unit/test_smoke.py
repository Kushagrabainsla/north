"""Smoke tests verifying the test harness itself is wired up.

Once real code lands, these can be deleted. Until then, they keep the
"add code + tests in the same change" rule (CODING_STYLE.md Section 23.4)
actionable from day one — there is always a green baseline to extend.
"""

from __future__ import annotations

import sys


def test_pytest_harness_collects_and_runs() -> None:
    assert True


def test_python_version_meets_requirement() -> None:
    assert sys.version_info >= (3, 12), (
        "north requires Python 3.12+ (see README.md Section 16.1 and pyproject.toml requires-python)."
    )
