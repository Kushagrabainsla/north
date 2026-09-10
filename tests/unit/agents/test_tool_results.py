from __future__ import annotations

import json

from agents.tool_results import (
    extract_success,
    failed_json,
    failure_kind,
    is_delegation_failure,
    is_unanswered_approval,
)


def test_success_and_failure_kind_fall_back_safely() -> None:
    assert extract_success(json.dumps({"success": True}))
    assert not extract_success(json.dumps({"success": False}))
    assert not extract_success("not json")

    assert failure_kind(json.dumps({"failure_kind": "refused"})) == "refused"
    assert failure_kind(json.dumps({"failure_kind": None})) == "error"
    assert failure_kind("not json") == "error"


def test_unanswered_approval_is_read_at_both_levels() -> None:
    assert is_unanswered_approval(json.dumps({"unanswered": True}))
    assert is_unanswered_approval(json.dumps({"data": {"unanswered": True}}))
    assert not is_unanswered_approval(json.dumps({"data": {}}))
    assert not is_unanswered_approval(json.dumps([1, 2]))
    assert not is_unanswered_approval("not json")


def test_delegation_failure_and_failed_payload() -> None:
    assert is_delegation_failure(json.dumps({"delegation_failed": True}))
    assert not is_delegation_failure(json.dumps({}))
    assert not is_delegation_failure("not json")

    assert json.loads(failed_json("boom")) == {"success": False, "error": "boom"}
