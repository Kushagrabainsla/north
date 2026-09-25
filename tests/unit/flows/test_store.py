"""Tests for durable flow run history."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from flows.store import FlowRunStore


def _finish(store: FlowRunStore, run_id: str, status: str = "completed") -> None:
    store.update(run_id, status=status, current_step=1, outputs=[])


def test_create_records_when_it_started_and_what_started_it(tmp_path):
    store = FlowRunStore(tmp_path / "runs.db")

    run = store.create(run_id="r1", flow_name="demo", trigger="schedule")

    assert run.trigger == "schedule"
    assert run.created_at
    assert store.get("r1").created_at == run.created_at


def test_list_runs_is_newest_first_and_can_be_narrowed_to_one_flow(tmp_path):
    store = FlowRunStore(tmp_path / "runs.db")
    store.create(run_id="a", flow_name="one")
    store.create(run_id="b", flow_name="two")
    store.create(run_id="c", flow_name="one")

    assert [run.run_id for run in store.list_runs()] == ["c", "b", "a"]
    assert [run.run_id for run in store.list_runs("one")] == ["c", "a"]
    assert [run.run_id for run in store.list_runs(limit=1)] == ["c"]


def test_list_runs_orders_by_start_not_by_last_update(tmp_path):
    store = FlowRunStore(tmp_path / "runs.db")
    store.create(run_id="early", flow_name="demo")
    store.create(run_id="late", flow_name="demo")
    _finish(store, "early")

    assert [run.run_id for run in store.list_runs()] == ["late", "early"]


def test_prune_deletes_only_finished_runs_older_than_the_cutoff(tmp_path):
    store = FlowRunStore(tmp_path / "runs.db")
    for run_id in ("done", "failed", "paused", "fresh"):
        store.create(run_id=run_id, flow_name="demo")
    _finish(store, "done")
    _finish(store, "failed", "failed")
    store.update("paused", status="paused", current_step=0, outputs=[])

    cutoff = datetime.now(UTC) + timedelta(seconds=1)
    assert store.prune(cutoff) == 2
    # A paused run can be resumed, and a run still going is not history yet.
    assert {run.run_id for run in store.list_runs()} == {"paused", "fresh"}

    assert store.prune(datetime.now(UTC) - timedelta(days=1)) == 0


def test_store_upgrades_a_database_written_before_history_was_kept(tmp_path):
    path = tmp_path / "runs.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE flow_runs (run_id TEXT PRIMARY KEY, flow_name TEXT NOT NULL, "
            "task_id TEXT NOT NULL DEFAULT '', agent TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, "
            "current_step INTEGER NOT NULL DEFAULT 0, inputs_json TEXT NOT NULL DEFAULT '{}', "
            "outputs_json TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO flow_runs (run_id, flow_name, status, updated_at) "
            "VALUES ('old', 'demo', 'completed', '2026-01-01T00:00:00+00:00')"
        )

    store = FlowRunStore(path)

    old = store.get("old")
    assert old.created_at == "" and old.trigger == ""
    assert [run.run_id for run in store.list_runs("demo")] == ["old"]


def test_runs_left_running_by_a_shutdown_are_failed_and_finished_ones_are_untouched(tmp_path):
    store = FlowRunStore(tmp_path / "runs.db")
    store.create(run_id="cut-off", flow_name="demo")
    store.create(run_id="done", flow_name="demo")
    store.create(run_id="waiting", flow_name="demo")
    _finish(store, "done")
    store.update("waiting", status="paused", current_step=0, outputs=[])

    assert store.fail_interrupted() == 1

    assert store.get("cut-off").status == "failed"
    assert "stopped" in store.get("cut-off").error
    assert store.get("done").status == "completed"
    assert store.get("waiting").status == "paused"
    assert store.fail_interrupted() == 0
