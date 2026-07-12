"""Tests for the policy primitive: authoritative, always-on operating rules.

Covers the loader (fail-closed on any malformed built-in policy), agent matching,
rendering, and the real shipped policies (safety binds every agent; clean-code binds
only coder+reviewer).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.policy import (
    Policy,
    PolicyError,
    load_policies,
    render_policies,
)

_REAL_POLICIES_DIR = Path(__file__).parent.parent.parent.parent / "policies"


def _write(dir_: Path, name: str, text: str) -> None:
    (dir_ / name).write_text(text, encoding="utf-8")


# --------------------------------------------------------------- parsing / matching


def test_load_policies_parses_star_and_list(tmp_path: Path) -> None:
    _write(tmp_path, "global.md", '---\napplies_to: "*"\n---\n## Global rule\nbe honest.')
    _write(tmp_path, "scoped.md", "---\napplies_to: [coder, reviewer]\n---\n## Code rule\nkeep it clean.")
    by_name = {p.name: p for p in load_policies(tmp_path)}

    assert by_name["global"].applies_to is None  # "*" => every agent
    assert by_name["scoped"].applies_to == frozenset({"coder", "reviewer"})
    assert "be honest." in by_name["global"].body


def test_policy_applies() -> None:
    everyone = Policy(name="s", applies_to=None, body="x")
    scoped = Policy(name="c", applies_to=frozenset({"coder"}), body="y")
    assert everyone.applies("finance") and everyone.applies("coder")
    assert scoped.applies("coder") and not scoped.applies("finance")


def test_render_concatenates_matching_bodies_and_is_empty_when_none() -> None:
    policies = [
        Policy(name="safety", applies_to=None, body="SAFETY"),
        Policy(name="clean", applies_to=frozenset({"coder"}), body="CLEAN"),
    ]
    coder_block = render_policies(policies, "coder")
    assert "SAFETY" in coder_block and "CLEAN" in coder_block
    # A non-code agent gets safety only, never clean-code.
    finance_block = render_policies(policies, "finance")
    assert "SAFETY" in finance_block and "CLEAN" not in finance_block
    # Nothing matching => empty string (appends cleanly to a prompt).
    assert render_policies([Policy("c", frozenset({"coder"}), "CLEAN")], "finance") == ""


# --------------------------------------------------------------- fail closed


def test_missing_directory_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(PolicyError):
        load_policies(tmp_path / "does_not_exist")


def test_empty_directory_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(PolicyError):
        load_policies(tmp_path)


def test_missing_frontmatter_fails_closed(tmp_path: Path) -> None:
    _write(tmp_path, "bad.md", "## No frontmatter here\njust a body")
    with pytest.raises(PolicyError):
        load_policies(tmp_path)


def test_empty_body_fails_closed(tmp_path: Path) -> None:
    _write(tmp_path, "bad.md", '---\napplies_to: "*"\n---\n   \n')
    with pytest.raises(PolicyError):
        load_policies(tmp_path)


def test_invalid_applies_to_fails_closed(tmp_path: Path) -> None:
    _write(tmp_path, "bad.md", "---\napplies_to: 123\n---\n## Rule\nbody")
    with pytest.raises(PolicyError):
        load_policies(tmp_path)


def test_invalid_yaml_fails_closed(tmp_path: Path) -> None:
    _write(tmp_path, "bad.md", "---\napplies_to: [unclosed\n---\n## Rule\nbody")
    with pytest.raises(PolicyError):
        load_policies(tmp_path)


def test_readme_is_ignored(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "# Docs, not a policy - no frontmatter")
    _write(tmp_path, "safety.md", '---\napplies_to: "*"\n---\n## Rule\nbody')
    names = {p.name for p in load_policies(tmp_path)}
    assert names == {"safety"}  # README skipped, not parsed as a (malformed) policy


# --------------------------------------------------------------- shipped policies


def test_shipped_policies_bind_the_right_agents() -> None:
    policies = load_policies(_REAL_POLICIES_DIR)
    by_name = {p.name: p for p in policies}
    assert "safety" in by_name and by_name["safety"].applies_to is None  # every agent
    assert by_name["clean-code"].applies_to == frozenset({"coder", "reviewer"})

    # Safety reaches every agent; clean-code only the two code agents.
    for agent in ("coder", "reviewer", "finance", "general", "researcher"):
        assert "non-negotiable" in render_policies(policies, agent)
    assert "Clean code" in render_policies(policies, "coder")
    assert "Clean code" not in render_policies(policies, "finance")
