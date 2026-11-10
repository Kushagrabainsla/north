from __future__ import annotations

import json

from agents.tool_results import (
    extract_success,
    failed_json,
    failure_kind,
)


def test_success_and_failure_kind_fall_back_safely() -> None:
    assert extract_success(json.dumps({"success": True}))
    assert not extract_success(json.dumps({"success": False}))
    assert not extract_success("not json")

    assert failure_kind(json.dumps({"failure_kind": "refused"})) == "refused"
    assert failure_kind(json.dumps({"failure_kind": None})) == "error"
    assert failure_kind("not json") == "error"


def test_failed_payload() -> None:
    assert json.loads(failed_json("boom")) == {"success": False, "error": "boom"}
