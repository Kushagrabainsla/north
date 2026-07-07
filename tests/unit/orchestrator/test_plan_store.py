"""Tests for PlanStore (#9 plan-and-track)."""

from __future__ import annotations

from orchestrator.plan_store import PlanStore


def test_set_and_render_plan():
    store = PlanStore()
    store.set_plan(
        "t1",
        [
            {"content": "Read the code", "status": "done"},
            {"content": "Write the fix", "status": "in_progress"},
            {"content": "Run tests", "status": "pending"},
        ],
    )
    rendered = store.render("t1")
    assert "[x] Read the code" in rendered
    assert "[~] Write the fix" in rendered
    assert "[ ] Run tests" in rendered


def test_progress_counts_done_over_total():
    store = PlanStore()
    store.set_plan("t1", [{"content": "a", "status": "done"}, {"content": "b"}])
    assert store.progress("t1") == (1, 2)


def test_set_plan_replaces_prior_plan():
    store = PlanStore()
    store.set_plan("t1", [{"content": "old"}])
    store.set_plan("t1", [{"content": "new"}])
    assert store.render("t1") == "[ ] new"


def test_invalid_status_defaults_to_pending():
    store = PlanStore()
    store.set_plan("t1", [{"content": "step", "status": "bogus"}])
    assert store.get_plan("t1")[0].status == "pending"


def test_step_alias_and_blank_filtering():
    store = PlanStore()
    steps = store.set_plan(
        "t1",
        [{"step": "via-alias"}, {"content": "   "}, {"content": "keep"}],
    )
    contents = [s.content for s in steps]
    assert contents == ["via-alias", "keep"]


def test_missing_task_is_empty():
    store = PlanStore()
    assert store.render("nope") == ""
    assert store.get_plan("nope") == []
    assert store.progress("nope") == (0, 0)


def test_clear_removes_plan():
    store = PlanStore()
    store.set_plan("t1", [{"content": "x"}])
    store.clear("t1")
    assert store.render("t1") == ""


def test_plans_are_task_scoped():
    store = PlanStore()
    store.set_plan("t1", [{"content": "one"}])
    store.set_plan("t2", [{"content": "two"}])
    assert store.render("t1") == "[ ] one"
    assert store.render("t2") == "[ ] two"
